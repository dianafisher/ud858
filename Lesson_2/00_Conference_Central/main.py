#!/usr/bin/env python
import webapp2
from google.appengine.api import app_identity
from google.appengine.api import mail
from google.appengine.api import memcache
from google.appengine.ext import ndb

from conference import ConferenceApi
from models import Session

class SetFeaturedSpeakerHandler(webapp2.RequestHandler):
    def post(self):
        """Checks if speaker has multiple sessions.
        If speaker has multiple sessions, place them in
        memcache as the featured speaker.
        """

        wsck = self.request.get('websafeConferenceKey')
        speaker = self.request.get('speaker')
        
        # Create an ancestor query to get the sessions for the conference
        sessions = Session.query(ancestor=ndb.Key(urlsafe=wsck))
        # Filter the sessions by speaker
        sessions = sessions.filter(Session.speaker == speaker)
        
        FEATURE_SPEAKER_TEMPLATE = ('Catch speaker %s at the following sessions: %s')

        if sessions.count > 1:
            announcement = FEATURE_SPEAKER_TEMPLATE % (speaker, ','.join(session.name for session in sessions))            
            memcache.set('FEATURED_SPEAKER', announcement)
        else:
            announcement = ""
            memcache.delete('FEATURED_SPEAKER')


class SetAnnouncementHandler(webapp2.RequestHandler):
    def get(self):
        """Set Announcement in Memcache."""        
        # uses _cacheAnnouncement() to set announcement in Memcache
        ConferenceApi._cacheAnnouncement()
        self.response.set_status(204)

class SendConfirmationEmailHandler(webapp2.RequestHandler):
    def post(self):
        """Send email confirming Conference creation."""
        mail.send_mail(
            'noreply@%s.appspotmail.com' % (
                app_identity.get_application_id()),     # from
            self.request.get('email'),                  # to
            'You created a new Conference!',            # subj
            'Hi, you have created a following '         # body
            'conference:\r\n\r\n%s' % self.request.get(
                'conferenceInfo')
        )        

app = webapp2.WSGIApplication([
    ('/crons/set_announcement', SetAnnouncementHandler),
    ('/tasks/set_featured_speaker', SetFeaturedSpeakerHandler),
    ('/tasks/send_confirmation_email', SendConfirmationEmailHandler),
], debug=True)