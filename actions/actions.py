import logging
from typing import Dict, Text, Any, List
from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher, Action
from rasa_sdk.forms import FormValidationAction
from rasa_sdk.events import (
    AllSlotsReset,
    SlotSet,
    SessionStarted,
    ActionExecuted,
    EventType,
)
from actions.snow import SnowAPI
import random
from pymongo import MongoClient

logger = logging.getLogger(__name__)
vers = "vers: 0.1.0, date: Apr 2, 2020"
logger.debug(vers)

snow = SnowAPI()
localmode = snow.localmode
logger.debug(f"Local mode: {snow.localmode}")


def getCollection(collectionName):

    # Provide the mongodb atlas url to connect python to mongodb using pymongo
    CONNECTION_STRING = "mongodb://localhost:27017/"
    # Create a connection using MongoClient. You can import MongoClient or use pymongo.MongoClient
    client = MongoClient(CONNECTION_STRING)
    db = client["local"]
    collection = db[collectionName]
    return collection


class ActionSessionStart(Action):
    def name(self) -> Text:
        return "action_session_start"

    @staticmethod
    def fetch_slots(tracker: Tracker) -> List[EventType]:
        """Collect slots that contain the user's name and phone number."""
        state = tracker.current_state()
        collection = getCollection("users")
        user = collection.find_one({"sender_id": state["sender_id"]})
        print(user)
        user_mail = user["email"]
        slots = []
        if user_mail:
            slots.append(SlotSet(key="previous_email", value=user_mail))
        return slots

    async def run(
        self, dispatcher, tracker: Tracker, domain: Dict[Text, Any]
    ) -> List[Dict[Text, Any]]:

        # the session should begin with a `session_started` event
        events = [SessionStarted()]

        # any slots that should be carried over should come after the
        # `session_started` event
        events.extend(self.fetch_slots(tracker))

        # an `action_listen` should be added at the end as a user message follows
        events.append(ActionExecuted("action_listen"))

        return events


class ActionAskEmail(Action):
    def name(self) -> Text:
        return "action_ask_email"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict]:
        if tracker.get_slot("previous_email"):
            dispatcher.utter_message(
                response=f"utter_ask_use_previous_email",
            )
        else:
            dispatcher.utter_message(response=f"utter_ask_email")
        return []


def _validate_email(
    value: Text,
    dispatcher: CollectingDispatcher,
    tracker: Tracker,
    domain: Dict[Text, Any],
) -> Dict[Text, Any]:
    """Validate email is in ticket system."""
    if not value:
        return {"email": None, "previous_email": None}
    elif isinstance(value, bool):
        value = tracker.get_slot("previous_email")

    if localmode:
        return {"email": value}

    results = snow.email_to_sysid(value)
    caller_id = results.get("caller_id")

    if caller_id:
        return {"email": value, "caller_id": caller_id}
    elif isinstance(caller_id, list):
        dispatcher.utter_message(response="utter_no_email")
        return {"email": None}
    else:
        dispatcher.utter_message(results.get("error"))
        return {"email": None}


class ValidateOpenIncidentForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_open_incident_form"

    def validate_email(
        self,
        value: Text,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        """Validate email is in ticket system."""
        return _validate_email(value, dispatcher, tracker, domain)

    def validate_priority(
        self,
        value: Text,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        """Validate priority is a valid value."""

        if value.lower() in snow.priority_db():
            return {"priority": value}
        else:
            dispatcher.utter_message(response="utter_no_priority")
            return {"priority": None}


class ActionOpenIncident(Action):
    def name(self) -> Text:
        return "action_open_incident"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict]:
        """Create an incident and return details or
        if localmode return incident details as if incident
        was created
        """

        priority = tracker.get_slot("priority")
        email = tracker.get_slot("email")
        problem_description = tracker.get_slot("problem_description")
        incident_title = tracker.get_slot("incident_title")
        confirm = tracker.get_slot("confirm")
        if not confirm:
            dispatcher.utter_message(
                response="utter_incident_creation_canceled"
            )
            return [AllSlotsReset(), SlotSet("previous_email", email)]

        if localmode:
            message = (
                f"An incident with the following details would be opened "
                f"if ServiceNow was connected:\n"
                f"email: {email}\n"
                f"problem description: {problem_description}\n"
                f"title: {incident_title}\npriority: {priority}"
            )
        else:
            snow_priority = snow.priority_db().get(priority)
            response = snow.create_incident(
                description=problem_description,
                short_description=incident_title,
                priority=snow_priority,
                email=email,
            )
            incident_number = (
                response.get("content", {}).get("result", {}).get("number")
            )
            if incident_number:
                message = (
                    f"Successfully opened up incident {incident_number} "
                    f"for you. Someone will reach out soon."
                )
                item = {
                    "incident_number": incident_number,
                    "mail": email,
                    "sender_id": tracker.current_state()["sender_id"],
                }
                collection = getCollection("users")
                collection.insert_one(item)
            else:
                message = (
                    f"Something went wrong while opening an incident for you. "
                    f"{response.get('error')}"
                )
        dispatcher.utter_message(message)
        return [AllSlotsReset(), SlotSet("previous_email", email)]


class IncidentStatusForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_incident_status_form"

    def validate_email(
        self,
        value: Text,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        """Validate email is in ticket system."""
        return _validate_email(value, dispatcher, tracker, domain)


class ActionCheckIncidentStatus(Action):
    def name(self) -> Text:
        return "action_check_incident_status"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict]:
        """Look up all incidents associated with email address
        and return status of each"""

        email = tracker.get_slot("email")

        incident_states = {
            "New": "is currently awaiting triage",
            "In Progress": "is currently in progress",
            "On Hold": "has been put on hold",
            "Closed": "has been closed",
        }
        if localmode:
            status = random.choice(list(incident_states.values()))
            message = (
                f"Since ServiceNow isn't connected, I'm making this up!\n"
                f"The most recent incident for {email} {status}"
            )
        else:
            incidents_result = snow.retrieve_incidents(email)
            incidents = incidents_result.get("incidents")
            if incidents:
                message = "\n".join(
                    [
                        f'Incident {i.get("number")}: '
                        f'"{i.get("short_description")}", '
                        f'opened on {i.get("opened_at")} '
                        f'{incident_states.get(i.get("incident_state"))}'
                        for i in incidents
                    ]
                )

            else:
                message = f"{incidents_result.get('error')}"

        dispatcher.utter_message(message)
        return [AllSlotsReset(), SlotSet("previous_email", email)]


class ValidateFeedbackForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_feedback_form"

    def validate_email(
        self,
        value: Text,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> Dict[Text, Any]:
        """Validate email is in ticket system."""
        return _validate_email(value, dispatcher, tracker, domain)


class ActionSendFeedback(Action):
    def name(self) -> Text:
        return "action_send_feedback"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict]:
        state = tracker.current_state()

        description = tracker.get_slot("feedback_description")
        email = tracker.get_slot("email")

        confirm = tracker.get_slot("confirm_feedback")
        if not confirm:
            dispatcher.utter_message(
                response="utter_feedback_sending_canceled"
            )
            return [AllSlotsReset(), SlotSet("previous_email", email)]

        item = {
            "text": description,
            "mail": email,
            "sender_id": state["sender_id"],
        }
        collection = getCollection("feedback")
        collection.insert_one(item)
        dispatcher.utter_message("Thank you for your feedback!")
        return [AllSlotsReset(), SlotSet("previous_email", email)]
