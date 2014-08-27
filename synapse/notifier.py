# -*- coding: utf-8 -*-
# Copyright 2014 matrix.org
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from twisted.internet import defer, reactor

from synapse.util.logutils import log_function

import logging


logger = logging.getLogger(__name__)


class _NotificationListener(object):
    """ This represents a single client connection to the events stream.

    The events stream handler will have yielded to the deferred, so to
    notify the handler it is sufficient to resolve the deferred.

    This listener will also keep track of which rooms it is listening in
    so that it can remove itself from the indexes in the Notifier class.
    """

    def __init__(self, user, rooms, from_token, limit, timeout, deferred):
        self.user = user
        self.from_token = from_token
        self.limit = limit
        self.timeout = timeout
        self.deferred = deferred

        self.rooms = rooms

        self.pending_notifications = []

    def notify(self, notifier, events, start_token, end_token):
        """ Inform whoever is listening about the new events. This will
        also remove this listener from all the indexes in the Notifier
        it knows about.
        """

        result = (events, (start_token, end_token))

        try:
            self.deferred.callback(result)
        except defer.AlreadyCalledError:
            pass

        for room in self.rooms:
            lst = notifier.rooms_to_listeners.get(room, set())
            lst.discard(self)

        notifier.user_to_listeners.get(self.user, set()).discard(self)


class Notifier(object):
    """ This class is responsible for notifying any listeners when there are
    new events available for it.

    Primarily used from the /events stream.
    """

    def __init__(self, hs):
        self.hs = hs

        self.rooms_to_listeners = {}
        self.user_to_listeners = {}

        self.event_sources = hs.get_event_sources()

        hs.get_distributor().observe(
            "user_joined_room", self._user_joined_room
        )

    @log_function
    @defer.inlineCallbacks
    def on_new_room_event(self, event, extra_users=[]):
        """ Used by handlers to inform the notifier something has happened
        in the room, room event wise.

        This triggers the notifier to wake up any listeners that are
        listening to the room, and any listeners for the users in the
        `extra_users` param.
        """
        room_id = event.room_id

        source = self.event_sources.sources["room"]

        listeners = self.rooms_to_listeners.get(room_id, set()).copy()

        for user in extra_users:
            listeners |= self.user_to_listeners.get(user, set()).copy()

        logger.debug("on_new_room_event listeners %s", listeners)

        # TODO (erikj): Can we make this more efficient by hitting the
        # db once?
        for listener in listeners:
            events, end_token = yield source.get_new_events_for_user(
                listener.user,
                listener.from_token,
                listener.limit,
            )

            if events:
                listener.notify(
                    self, events, listener.from_token, end_token
                )

    @defer.inlineCallbacks
    def on_new_user_event(self, users=[], rooms=[]):
        """ Used to inform listeners that something has happend
        presence/user event wise.

        Will wake up all listeners for the given users and rooms.
        """
        source = self.event_sources.sources["presence"]

        listeners = set()

        for user in users:
            listeners |= self.user_to_listeners.get(user, set()).copy()

        for room in rooms:
            listeners |= self.rooms_to_listeners.get(room, set()).copy()

        for listener in listeners:
            events, end_token = yield source.get_new_events_for_user(
                listener.user,
                listener.from_token,
                listener.limit,
            )

            if events:
                listener.notify(
                    self, events, listener.from_token, end_token
                )

    def get_events_for(self, user, rooms, pagination_config, timeout):
        """ For the given user and rooms, return any new events for them. If
        there are no new events wait for up to `timeout` milliseconds for any
        new events to happen before returning.
        """
        deferred = defer.Deferred()

        self._get_events(
            deferred, user, rooms, pagination_config.from_token,
            pagination_config.limit, timeout
        ).addErrback(deferred.errback)

        return deferred

    @defer.inlineCallbacks
    def _get_events(self, deferred, user, rooms, from_token, limit, timeout):
        if not from_token:
            from_token = yield self.event_sources.get_current_token()

        listener = _NotificationListener(
            user,
            rooms,
            from_token,
            limit,
            timeout,
            deferred,
        )

        if timeout:
            reactor.callLater(timeout/1000, self._timeout_listener, listener)

            self._register_with_keys(listener)

        yield self._check_for_updates(listener)

        if not timeout:
            self._timeout_listener(listener)

        return

    def _timeout_listener(self, listener):
        # TODO (erikj): We should probably set to_token to the current max
        # rather than reusing from_token.
        listener.notify(
            self,
            [],
            listener.from_token,
            listener.from_token,
        )

    @log_function
    def _register_with_keys(self, listener):
        for room in listener.rooms:
            s = self.rooms_to_listeners.setdefault(room, set())
            s.add(listener)

        self.user_to_listeners.setdefault(listener.user, set()).add(listener)

    @defer.inlineCallbacks
    @log_function
    def _check_for_updates(self, listener):
        # TODO (erikj): We need to think about limits across multiple sources
        events = []

        from_token = listener.from_token
        limit = listener.limit

        # TODO (erikj): DeferredList?
        for source in self.event_sources.sources.values():
            stuff, new_token = yield source.get_new_events_for_user(
                listener.user,
                from_token,
                limit,
            )

            events.extend(stuff)

            from_token = new_token

        end_token = from_token

        if events:
            listener.notify(self, events, listener.from_token, end_token)

        defer.returnValue(listener)

    def _user_joined_room(self, user, room_id):
        new_listeners = self.user_to_listeners.get(user, set())

        listeners = self.rooms_to_listeners.setdefault(room_id, set())
        listeners |= new_listeners