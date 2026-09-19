import unittest
from copy import deepcopy
from unittest.mock import Mock

from slack_bot import MessageReceiver, Settings


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings("xoxb-test", "xapp-test", "Ttarget", "Atarget", "Ctarget")
        self.receiver = MessageReceiver(self.settings, "Ubot")
        self.client = Mock()
        self.body = {
            "team_id": "Ttarget", "api_app_id": "Atarget", "event_id": "Ev1",
            "event": {"type": "message", "channel": "Ctarget", "user": "Uhuman", "text": "ping", "ts": "100.1"},
        }

    def test_ping_without_mention_replies_in_its_thread(self):
        self.receiver.receive(self.body, self.client)
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs["thread_ts"], "100.1")
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs["channel"], "Ctarget")

    def test_thread_reply_stays_in_existing_thread(self):
        self.body["event"]["thread_ts"] = "99.1"
        self.receiver.receive(self.body, self.client)
        self.assertEqual(self.client.chat_postMessage.call_args.kwargs["thread_ts"], "99.1")

    def test_test_also_triggers_a_connection_reply(self):
        self.body["event"]["text"] = " Test "
        self.receiver.receive(self.body, self.client)
        self.client.chat_postMessage.assert_called_once()

    def test_ordinary_messages_are_received_without_a_test_reply(self):
        self.body["event"]["text"] = "Need more gloves"
        with self.assertLogs("labpurchase", level="INFO") as logs:
            self.receiver.receive(self.body, self.client)
        self.assertIn("Received channel message", logs.output[0])
        self.assertNotIn("gloves", logs.output[0])
        self.client.chat_postMessage.assert_not_called()

    def test_outside_workspace_app_or_channel_is_ignored(self):
        for key in ("team_id", "api_app_id", "channel"):
            body = deepcopy(self.body)
            (body["event"] if key == "channel" else body)[key] = "other"
            self.receiver.receive(body, self.client)
        self.client.chat_postMessage.assert_not_called()

    def test_own_bot_other_bots_and_message_edits_do_not_loop(self):
        for extra in ({"user": "Ubot"}, {"bot_id": "Bother"}, {"subtype": "bot_message"}, {"subtype": "message_changed"}, {"subtype": "message_deleted"}, {"subtype": "channel_join"}):
            body = deepcopy(self.body)
            body["event"].update(extra)
            self.receiver.receive(body, self.client)
        self.client.chat_postMessage.assert_not_called()

    def test_retry_and_broadcast_do_not_duplicate_reply(self):
        self.receiver.receive(self.body, self.client)
        self.receiver.receive(self.body, self.client)
        broadcast = deepcopy(self.body)
        broadcast["event_id"] = "Ev2"
        broadcast["event"]["subtype"] = "thread_broadcast"
        self.receiver.receive(broadcast, self.client)
        self.client.chat_postMessage.assert_called_once()

    def test_file_only_message_is_received(self):
        self.body["event"].update(subtype="file_share", text="", files=[{"id": "F1"}])
        with self.assertLogs("labpurchase", level="INFO"):
            self.receiver.receive(self.body, self.client)
        self.client.chat_postMessage.assert_not_called()

    def test_images_are_intaken_without_an_at_mention(self):
        intake = Mock()
        intake.capture.return_value = {"status": "awaiting_ai", "files": [{"file_id": "F1"}]}
        receiver = MessageReceiver(self.settings, "Ubot", intake)
        self.body["event"].update(text="Put it in 365", files=[{"id": "F1"}])
        receiver.receive(self.body, self.client)
        intake.capture.assert_called_once_with(self.body["event"], self.client, self.settings.bot_token)
        self.client.chat_postMessage.assert_not_called()

    def test_shortage_photos_reach_semantic_reader_without_immediate_reply(self):
        intake = Mock()
        intake.capture.return_value = None
        receiver = MessageReceiver(self.settings, 'Ubot', intake)
        self.body['event'].update(text='Midiprep P2 runs out.', files=[{'id': 'F1'}])
        receiver.receive(self.body, self.client)
        intake.capture.assert_called_once()
        self.client.chat_postMessage.assert_not_called()

    def test_order_question_routes_before_shortage_filter_without_a_mention(self):
        intake=Mock();queries=Mock();queries.capture.return_value=True
        receiver=MessageReceiver(self.settings,'Ubot',intake,queries)
        self.body['event'].update(text='Running out of tubes. What is the status of order A123?')
        receiver.receive(self.body,self.client)
        queries.capture.assert_called_once_with(self.body['event'])
        intake.capture.assert_not_called();self.client.chat_postMessage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
