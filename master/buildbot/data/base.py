# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import copy
import enum
import functools
import re
from collections import UserList

from twisted.internet import defer

from buildbot.data import exceptions
from buildbot.util.twisted import async_to_deferred


class EndpointKind(enum.Enum):
    SINGLE = 1
    COLLECTION = 2
    RAW = 3
    RAW_INLINE = 4


class ResourceType:
    name = None
    plural = None
    endpoints = []
    keyField = None
    eventPathPatterns = ""
    entityType = None
    subresources = []

    def __init__(self, master):
        self.master = master
        self.compileEventPathPatterns()

    def compileEventPathPatterns(self):
        # We'll run a single format, and then split the string
        # to get the final event path tuple
        pathPatterns = self.eventPathPatterns
        pathPatterns = pathPatterns.split()
        identifiers = re.compile(r':([^/]*)')
        for i, pp in enumerate(pathPatterns):
            pp = identifiers.sub(r'{\1}', pp)
            if pp.startswith("/"):
                pp = pp[1:]
            pathPatterns[i] = pp
        self.eventPaths = pathPatterns

    @functools.lru_cache(1)
    def getEndpoints(self):
        endpoints = self.endpoints[:]
        for i, ep in enumerate(endpoints):
            if not issubclass(ep, Endpoint):
                raise TypeError("Not an Endpoint subclass")
            endpoints[i] = ep(self, self.master)
        return endpoints

    @functools.lru_cache(1)
    def getDefaultEndpoint(self):
        for ep in self.getEndpoints():
            if ep.kind != EndpointKind.COLLECTION:
                return ep
        return None

    @functools.lru_cache(1)
    def getCollectionEndpoint(self):
        for ep in self.getEndpoints():
            if ep.kind == EndpointKind.COLLECTION or ep.isPseudoCollection:
                return ep
        return None

    @staticmethod
    def sanitizeMessage(msg):
        msg = copy.deepcopy(msg)
        return msg

    def produceEvent(self, msg, event):
        if msg is not None:
            msg = self.sanitizeMessage(msg)
            for path in self.eventPaths:
                path = path.format(**msg)
                routingKey = tuple(path.split("/")) + (event,)
                self.master.mq.produce(routingKey, msg)


class SubResource:
    def __init__(self, rtype):
        self.rtype = rtype
        self.endpoints = {}
        for endpoint in rtype.endpoints:
            if endpoint.kind == EndpointKind.COLLECTION:
                self.endpoints[rtype.plural] = endpoint
            else:
                self.endpoints[rtype.name] = endpoint


class Endpoint:
    pathPatterns = ""
    rootLinkName = None
    isPseudoCollection = False
    kind = EndpointKind.SINGLE
    parentMapping = {}

    def __init__(self, rtype, master):
        self.rtype = rtype
        self.master = master

    def get(self, resultSpec, kwargs):
        raise NotImplementedError

    def control(self, action, args, kwargs):
        # we convert the action into a mixedCase method name
        action_method = getattr(self, "action" + action.capitalize(), None)
        if action_method is None:
            raise exceptions.InvalidControlException(f"action: {action} is not supported")
        return action_method(args, kwargs)

    def get_kwargs_from_graphql_parent(self, parent, parent_type):
        if parent_type not in self.parentMapping:
            rtype = self.master.data.getResourceTypeForGraphQlType(parent_type)
            if rtype.keyField in parent:
                parentid = rtype.keyField
            else:
                raise NotImplementedError(
                    "Collection endpoint should implement "
                    "get_kwargs_from_graphql or parentMapping"
                )
        else:
            parentid = self.parentMapping[parent_type]
        ret = {'graphql': True}
        ret[parentid] = parent[parentid]
        return ret

    def get_kwargs_from_graphql(self, parent, resolve_info, args):
        if self.kind == EndpointKind.COLLECTION or self.isPseudoCollection:
            if parent is not None:
                return self.get_kwargs_from_graphql_parent(parent, resolve_info.parent_type.name)
            return {'graphql': True}
        ret = {'graphql': True}
        k = self.rtype.keyField
        v = args.pop(k)
        if v is not None:
            ret[k] = v
        return ret

    def __repr__(self):
        return "endpoint for " + ",".join(self.pathPatterns.split())


class NestedBuildDataRetriever:
    """
    Efficiently retrieves data about various entities without repeating same queries over and over.
    The following arg keys are supported:
        - stepid
        - step_name
        - step_number
        - buildid
        - build_number
        - builderid
        - buildername
        - logid
        - log_slug
    """

    __slots__ = (
        'master',
        'args',
        'step_dict',
        'build_dict',
        'builder_dict',
        'log_dict',
        'worker_dict',
    )

    def __init__(self, master, args):
        self.master = master
        self.args = args
        # False is used as special value as "not set". None is used as "not exists". This solves
        # the problem of multiple database queries in case entity does not exist.
        self.step_dict = False
        self.build_dict = False
        self.builder_dict = False
        self.log_dict = False
        self.worker_dict = False

    @async_to_deferred
    async def get_step_dict(self):
        if self.step_dict is not False:
            return self.step_dict

        if 'stepid' in self.args:
            self.step_dict = await self.master.db.steps.getStep(stepid=self.args['stepid'])
            return self.step_dict

        if 'step_name' in self.args or 'step_number' in self.args:
            build_dict = await self.get_build_dict()
            if build_dict is None:
                self.step_dict = None
                return None

            self.step_dict = await self.master.db.steps.getStep(
                buildid=build_dict['id'],
                number=self.args.get('step_number'),
                name=self.args.get('step_name'),
            )
            return self.step_dict

        # fallback when there's only indirect information
        if 'logid' in self.args:
            log_dict = await self.get_log_dict()
            if log_dict is not None:
                self.step_dict = await self.master.db.steps.getStep(stepid=log_dict['stepid'])
                return self.step_dict

        self.step_dict = None
        return self.step_dict

    @async_to_deferred
    async def get_build_dict(self):
        if self.build_dict is not False:
            return self.build_dict

        if 'buildid' in self.args:
            self.build_dict = await self.master.db.builds.getBuild(self.args['buildid'])
            return self.build_dict

        if 'build_number' in self.args:
            builder_dict = await self.get_builder_dict()

            if builder_dict is None:
                self.build_dict = None
                return None

            self.build_dict = await self.master.db.builds.getBuildByNumber(
                builderid=builder_dict['id'], number=self.args['build_number']
            )
            return self.build_dict

        # fallback when there's only indirect information
        step_dict = await self.get_step_dict()
        if step_dict is not None:
            self.build_dict = await self.master.db.builds.getBuild(step_dict['buildid'])
            return self.build_dict

        self.build_dict = None
        return None

    @async_to_deferred
    async def get_build_id(self):
        if 'buildid' in self.args:
            return self.args['buildid']

        build_dict = await self.get_build_dict()
        if build_dict is None:
            return None
        return build_dict['id']

    @async_to_deferred
    async def get_builder_dict(self):
        if self.builder_dict is not False:
            return self.builder_dict

        if 'builderid' in self.args:
            self.builder_dict = await self.master.db.builders.getBuilder(self.args['builderid'])
            return self.builder_dict

        if 'buildername' in self.args:
            builder_id = await self.master.db.builders.findBuilderId(
                self.args['buildername'], autoCreate=False
            )
            builder_dict = None
            if builder_id is not None:
                builder_dict = await self.master.db.builders.getBuilder(builder_id)
            self.builder_dict = builder_dict
            return self.builder_dict

        # fallback when there's only indirect information
        build_dict = await self.get_build_dict()
        if build_dict is not None:
            self.builder_dict = await self.master.db.builders.getBuilder(build_dict['builderid'])
            return self.builder_dict

        self.builder_dict = None
        return None

    @async_to_deferred
    async def get_builder_id(self):
        if 'builderid' in self.args:
            return self.args['builderid']

        builder_dict = await self.get_builder_dict()
        if builder_dict is None:
            return None
        return builder_dict['id']

    @async_to_deferred
    async def get_log_dict(self):
        if self.log_dict is not False:
            return self.log_dict

        if 'logid' in self.args:
            self.log_dict = await self.master.db.logs.getLog(self.args['logid'])
            return self.log_dict

        step_dict = await self.get_step_dict()
        if step_dict is None:
            self.log_dict = None
            return None
        self.log_dict = await self.master.db.logs.getLogBySlug(
            step_dict['id'], self.args.get('log_slug')
        )
        return self.log_dict

    @async_to_deferred
    async def get_log_id(self):
        if 'logid' in self.args:
            return self.args['logid']

        log_dict = await self.get_log_dict()
        if log_dict is None:
            return None
        return log_dict['id']

    @async_to_deferred
    async def get_worker_dict(self):
        if self.worker_dict is not False:
            return self.worker_dict

        build_dict = await self.get_build_dict()
        if build_dict is not None:
            workerid = build_dict.get('workerid', None)
            if workerid is not None:
                self.worker_dict = await self.master.db.workers.getWorker(workerid=workerid)
                return self.worker_dict

        self.worker_dict = None
        return None


class BuildNestingMixin:
    """
    A mixin for methods to decipher the many ways a various entities can be specified.
    """

    @defer.inlineCallbacks
    def getBuildid(self, kwargs):
        retriever = NestedBuildDataRetriever(self.master, kwargs)
        return (yield retriever.get_build_id())

    @defer.inlineCallbacks
    def getBuilderId(self, kwargs):
        retriever = NestedBuildDataRetriever(self.master, kwargs)
        return (yield retriever.get_builder_id())

    # returns Deferred that yields a number
    def get_project_id(self, kwargs):
        if "projectname" in kwargs:
            return self.master.db.projects.find_project_id(kwargs["projectname"], auto_create=False)
        return defer.succeed(kwargs["projectid"])


class ListResult(UserList):
    __slots__ = ['offset', 'total', 'limit']

    def __init__(self, values, offset=None, total=None, limit=None):
        super().__init__(values)

        # if set, this is the index in the overall results of the first element of
        # this list
        self.offset = offset

        # if set, this is the total number of results
        self.total = total

        # if set, this is the limit, either from the user or the implementation
        self.limit = limit

    def __repr__(self):
        return (
            f"ListResult({repr(self.data)}, offset={repr(self.offset)}, "
            f"total={repr(self.total)}, limit={repr(self.limit)})"
        )

    def __eq__(self, other):
        if isinstance(other, ListResult):
            return (
                self.data == other.data
                and self.offset == other.offset
                and self.total == other.total
                and self.limit == other.limit
            )
        return (
            self.data == other
            and self.offset is None
            and self.limit is None
            and (self.total is None or self.total == len(other))
        )

    def __ne__(self, other):
        return not self == other


def updateMethod(func):
    """Decorate this resourceType instance as an update method, made available
    at master.data.updates.$funcname"""
    func.isUpdateMethod = True
    return func
