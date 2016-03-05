#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime
import datetime as dt

import json
import os
import time

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import urlfetch
from google.appengine.ext import ndb
from google.appengine.api import memcache
from google.appengine.api import taskqueue

from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize
from models import Session

from settings import WEB_CLIENT_ID
from utils import getUserId

from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import BooleanMessage
from models import ConflictException
from models import StringMessage
from models import SessionForm
from models import SessionForms
from models import SessionType
from models import SessionTypeForm
from models import SessionSpeakerForm
from models import SessionCityForm

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_FEATURED_SPEAKER_KEY = "FEATURED_SPEAKER"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees',
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)         

SESSION_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1),
)   

SESSION_TYPE_GET_REQUEST = endpoints.ResourceContainer(
    SessionTypeForm,
    websafeConferenceKey=messages.StringField(1),    
)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

@endpoints.api( name='conference',
                version='v1',
                allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
                scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        ## TODO 2
        ## step 1: make sure user is authed
        ## uncomment the following lines:
        # Use endpoints AUTH to get the current user.
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        user_id = getUserId(user)
        
        # create a new key of kind Profile from the id
        p_key = ndb.Key(Profile, user_id)

        # get the entity from datastore by using get() on the key
        profile = p_key.get()

        # profile = None
        ## step 2: create a new Profile from logged in user data
        ## you can use user.nickname() to get displayName
        ## and user.email() to get mainEmail
        if not profile:
            profile = Profile(                
                key = p_key,
                displayName = user.nickname(), 
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            # save the profile to the datastore    
            profile.put()
    
        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()
        print 'in _doProfile...'
        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    print '#####'
                    print field, val
                    print '#####'
                    if val:
                        setattr(prof, field, str(val))

            # put the modified profile to datastore
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()

    # TODO 1
    # 1. change request class
    # 2. pass request to _doProfile function
    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""    

        return self._doProfile(request)

# - - - Conference objects - - - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id

        # create Conference & return (modified) ConferenceForm
        Conference(**data).put()

        # Send confirmation email.
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)

    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        print 'queryConferences'
        conferences = self._getQuery(request)
        # return individual ConferenceForm object per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") \
            for conf in conferences]
        )   

    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))          

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='getConferencesCreated',
        http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # make profile key
        p_key = ndb.Key(Profile, getUserId(user))
        # create ancestor query for this user
        conferences = Conference.query(ancestor=p_key)
        # get the user profile and display name
        prof = p_key.get()
        displayName = getattr(prof, 'displayName')
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, displayName) for conf in conferences]
        )

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
        path='filterPlayground',
        http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        q = Conference.query()
        # simple filter usage:
        # q = q.filter(Conference.city == "Paris")

        # advanced filter building and usage
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)

        # # TODO
        # # add 2 filters:
        # # 1: city equals to London
        # q = q.filter(Conference.city == "London")
        # # 2: topic equals "Medical Innovations"
        # q = q.filter(Conference.topics == "Medical Innovations")
        # # 3: order by conference name
        # q = q.order(Conference.name)

        # # f = ndb.query.FilterNode("topics", "=", "Medical Innovations")
        # # q = q.filter(f)

        # # 4: filter by month
        # # q = q.filter(Conference.month == 2)

        # # 5: filter for big conferences
        # q = q.filter(Conference.maxAttendees > 3)

        q = Conference.query().\
            filter(Conference.city == "London").\
            filter(Conference.seatsAvailable >= 1).\
            filter(Conference.seatsAvailable <= 9).\
            order(Conference.seatsAvailable).\
            order(Conference.name).\
            order(Conference.month)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        print '_getQuery'
        print request
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None
        print filters
        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)    

    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        # TODO:
        # step 1: get user profile
        profile = self._getProfileFromUser()
        # step 2: get conferenceKeysToAttend from profile.
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in profile.conferenceKeysToAttend]        
        # to make a ndb key from websafe key you can use:
        # ndb.Key(urlsafe=my_websafe_key_string)
        # step 3: fetch conferences from datastore. 
        # Use get_multi(array_of_keys) to fetch all keys at once.
        # Do not fetch them one by one!
        conferences = ndb.get_multi(conf_keys)
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, "")\
         for conf in conferences]
        )


# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""        
        # return an existing announcement from Memcache or an empty string.
        # announcement = ""
        # return StringMessage(data=announcement)
        return StringMessage(data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")

# - - - Session objects - - - - - - - - - - - - - - - - - - -

    @endpoints.method(
        request_message=SessionForm, 
        response_message=SessionForm,
        path='session',
        http_method='POST', 
        name='createSession')
    def createSession(self, request):        
        return self._createSessionObject(request)        

    def _createSessionObject(self, request):
        """Create or update session object. """

        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")       

        # Fetch the conference from the request
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()

        # Make sure the conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: \
                %s' % request.websafeConferenceKey)

        # print 'conference', conf.name  

        # Make sure the user owns the conference
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the conference owner can add sessions.')          

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}        

        # print 'data', data            
        # Default session type to NOT_SPECIFIED if no type was provided.
        if data['typeOfSession']:
            data['typeOfSession'] = str(data['typeOfSession'])
        else:
            data['typeOfSession'] = str(SessionType.NOT_SPECIFIED)
         

        # Convert dates from strings to Date objects
        if data['date']:
            try:
                data['date'] = datetime.strptime(
                    data['date'][:10], "%Y-%m-%d").date()
            except ValueError:
                raise endpoints.BadRequestException('Date must have format YYYY-MM-DD')

        # convert time from strings to Time object
        if data['startTime']:            
            if len(data['startTime']) is not 5:
                raise endpoints.BadRequestException('Start time must be HH:MM using 24 hour notation')            
            data['startTime'] = datetime.strptime(
                data['startTime'][:5], "%H:%M").time()            

        # Make Session Key from Conference ID as p_key
        p_key = ndb.Key(urlsafe=request.websafeConferenceKey)

        # Allocate new Session ID with p_key as parent
        s_id = Session.allocate_ids(size=1, parent=p_key)[0]

        s_key = ndb.Key(Session, s_id, parent=p_key)
        data['key'] = s_key

        # Remove items which are not part of the Session object.
        del data['websafeConferenceKey']
        del data['websafeKey']
        
        # create Session
        Session(**data).put()        

        # Get the speaker for the new session
        speaker = data['speaker']
        # Add speaker to memcache to support Featured Speaker.
        taskqueue.add(params={
            "speaker": speaker,
            "websafeConferenceKey": wsck
            }, url="/tasks/set_featured_speaker")

        return request

    def _copySessionToForm(self, session):
        """Copy relevant fields from Session to SessionForm"""

        sessionForm = SessionForm()
        for field in sessionForm.all_fields():
            if hasattr(session, field.name):                
                # Convert date and time to String
                if field.name.endswith('date') or field.name.endswith('Time'):                    
                    setattr(sessionForm, field.name, str(getattr(session, field.name)))
                elif field.name == 'typeOfSession':
                    setattr(sessionForm, field.name, getattr(SessionType, getattr(session, field.name)))                           
                else:
                    setattr(sessionForm, field.name, getattr(session, field.name))            
            elif field.name == 'websafeKey':
                    setattr(sessionForm, field.name, session.key.urlsafe())                             
            
        sessionForm.check_initialized()
        return sessionForm

    @endpoints.method(
        request_message=CONF_GET_REQUEST,
        response_message=SessionForms,
        path='conference/{websafeConferenceKey}/sessions',
        http_method='GET',
        name='getConferenceSessions'
        )
    def getConferenceSessions(self, request):
        """Return all sessions for a given conference"""


        # Fetch the conference from the request
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()

        # Make sure the conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: \
                %s' % request.websafeConferenceKey)

        # create ancestor query
        sessions = Session.query(ancestor=ndb.Key(urlsafe=wsck))
        return SessionForms(
            sessions=[self._copySessionToForm(s) for s in sessions]
        )
        
    @endpoints.method(
        request_message=SESSION_TYPE_GET_REQUEST,
        response_message=SessionForms,
        path='conference/{websafeConferenceKey}/sessions/type',
        http_method='GET',
        name='getConferenceSessionsByType'
        )
    def getConferenceSessionsByType(self, request):
        """Returns session filtered by type for a given conference"""

        # Fetch the conference from the request
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()

        # Make sure the conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: \
                %s' % request.websafeConferenceKey)

        # create ancestor query
        sessions = Session.query(ancestor=ndb.Key(urlsafe=wsck))            
        # Filter by type        
        sessions = sessions.filter(Session.typeOfSession == str(request.sessionType))
        
        # Return set of SessionForm objects
        return SessionForms(
            sessions=[self._copySessionToForm(s) for s in sessions]
        )


    @endpoints.method(
        request_message=SessionSpeakerForm,
        response_message=SessionForms,
        path='conference/sessions/speaker',
        http_method='GET',
        name='getSessionsBySpeaker'
        )
    def getSessionsBySpeaker(self, request):
        """Returns all sessions given by a particular speaker, across all conferences"""

        # Create query
        sessions = Session.query()
        # Filter by speaker
        sessions = sessions.filter(Session.speaker == request.speaker)

        # Return set of SessionForm objects
        return SessionForms(
            sessions=[self._copySessionToForm(s) for s in sessions]
        )

# - - - Wishlist - - - - - - - - - - - - - - - - - - - -        

    @endpoints.method(
        request_message=SESSION_GET_REQUEST,
        response_message=BooleanMessage,
        path='conference/session/{websafeSessionKey}/wishlist',
        http_method='POST',
        name='addSessionToWishlist'
        )
    def addSessionToWishList(self, request):
        """Add session to user's wishlist"""
        return self._addToWishlist(request)

    @endpoints.method(
        request_message=SESSION_GET_REQUEST,
        response_message=BooleanMessage,
        path='conference/session/{websafeSessionKey}/wishlist',
        http_method='DELETE',
        name='removeSessionFromWishlist'
        )
    def removeSessionFromWishlist(self, request):
        """Remove session from user's wishlist"""
        return self._addToWishlist(request, add=False)


    @ndb.transactional()
    def _addToWishlist(self, request, add=True):
        """Add/remove sessions from user wishlist"""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        wssk = request.websafeSessionKey
        session = ndb.Key(urlsafe=wssk).get()
        if not session:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wssk)

        # Add to wishlist
        if add:
            # Check if user already added session to wishlist
            if wssk in prof.sessionKeysWishlist:
                raise ConflictException(
                    'You have already added this session to your wishlist')

            # Add session to user wishlist
            prof.sessionKeysWishlist.append(wssk)
            retval = True

        # Remove from wishlist
        else:
            if wssk in prof.sessionKeysWishlist:
                prof.sessionKeysWishlist.remove(wssk)
                retval = True
            else:
                retval = False

        # Write the profile back to the datastore.
        prof.put()

        return BooleanMessage(data=retval)

    @endpoints.method(
        request_message=message_types.VoidMessage,
        response_message=SessionForms,
        path='conference/sessions/attending',
        http_method='GET',
        name='getSessionsInWishlist'
        )
    def getSessionsInWishlist(self, request):
        """Get list of sessions in a conference that the user has in their wishlist"""
        # Get user Profile
        prof = self._getProfileFromUser()
        # Get list of session keys in the wishlist
        session_keys = [ndb.Key(urlsafe=wssk) for wssk in prof.sessionKeysWishlist]
        # Get the Session entities from the Datastore
        sessions = ndb.get_multi(session_keys)

        # Return set of SessionForm objects
        return SessionForms(sessions=[self._copySessionToForm(s) for s in sessions])

# - - - Additional Session Queries - - - - - - - - - - - - - - - - - - - - 

    @endpoints.method(
        request_message=SessionCityForm,
        response_message=SessionForms,
        path='conference/sessions/city',
        http_method='GET',
        name='getSessionsByCity'
        )
    def getSessionByCity(self, request):
        """Returns all sessions for a given city, across all conferences"""
        # Create a list to hold all of the sessions found.
        results = []
        # Create conference query
        conferences = Conference.query()
        # Filter conferences by city
        conferences = conferences.filter(Conference.city == request.city)
        # Create ancestor query to find session for each conference.
        for conf in conferences:                        
            # Create an ancestor query to get the sessions for this conference
            sessions = Session.query(ancestor=conf.key)
            # Add each session in the query to the results list
            for s in sessions:
                # print s.name
                results.append(s)                                    
                            
        # Return set of SessionForm objects
        return SessionForms(sessions=[self._copySessionToForm(s) for s in results])

    @endpoints.method(
        request_message=CONF_GET_REQUEST,
        response_message=SessionForms,
        path='conference/{websafeConferenceKey}/sessions/ordered',
        http_method='GET',
        name='getConferenceSessionsOrdered'
        )
    def getConferenceSessionsOrderedByDate(self, request):
        """Returns sessions of a conference ordered by session date and time"""
        # Fetch the conference from the request
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()

        # Make sure the conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: \
                %s' % request.websafeConferenceKey)
        
        # create ancestor query
        sessions = Session.query(ancestor=ndb.Key(urlsafe=wsck))                 
        # First, order by date
        sessions = sessions.order(Session.date)
        # then order by startTime
        sessions = sessions.order(Session.startTime)

        # Return set of SessionForm objects
        return SessionForms(
            sessions=[self._copySessionToForm(s) for s in sessions]
        )

    @endpoints.method(
        request_message=message_types.VoidMessage,
        response_message=SessionForms,
        path='conference/sessions/nonworkshops',
        http_method='GET',
        name='getNonWorkshopSessionsBefore7'
        )
    def getNonWorkshopSessionsBefore7(self, request):
        """Returns all sessions which are not of type WORKSHOP and have a startTime before 7PM"""
        # Create sessions query
        sessions = Session.query()
        # Filter by type (any type except WORKSHOP)
        # Can only use the inequality operation on one field per query.  So we cannot use both
        # != for session type and < for startTime.  Use ndb.OR instead for the session type filter.

        sessions = sessions.filter(ndb.OR(
            Session.typeOfSession == str(SessionType.KEYNOTE),
            Session.typeOfSession == str(SessionType.LECTURE),
            Session.typeOfSession == str(SessionType.NOT_SPECIFIED)))
        
        # Filter by time (any time before 19:00)
        seven = dt.time(19, 00)
        sessions = sessions.filter(Session.startTime < seven)

        # Return set of SessionForm objects
        return SessionForms(
            sessions=[self._copySessionToForm(s) for s in sessions]
        )

# - - - Featured Speaker - - - - - - - - - - - - - - - - - - - -         

    @endpoints.method(
        request_message=message_types.VoidMessage,
        response_message=StringMessage,
        path='featuredSpeakers',
        http_method='GET',
        name='getFeaturedSpeaker'        
        )
    def getFeaturedSpeaker(self, request):
        """Returns Featured Speaker from memcache."""
        return StringMessage(data=memcache.get(MEMCACHE_FEATURED_SPEAKER_KEY) or "")



# registers API
api = endpoints.api_server([ConferenceApi]) 
