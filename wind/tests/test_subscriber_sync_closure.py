from django.test import TestCase
from django.utils import timezone

from wind.models import ListOfSubscriber
from wind.functions.getSubscriber import (
    _delete_local_subscribers_not_in_remote,
    _is_closure_tombstone,
    _update_subscriber_from_row,
)


class SubscriberSyncClosureProtectionTestCase(TestCase):
    def test_closed_tombstone_is_detected(self):
        sub = ListOfSubscriber(
            id="C1",
            code="C1",
            status=ListOfSubscriber.STATUS_CLOSED,
        )
        self.assertTrue(_is_closure_tombstone(sub))

    def test_pending_closure_tombstone_is_detected(self):
        sub = ListOfSubscriber(
            id="P1",
            code="P1",
            status=ListOfSubscriber.STATUS_PENDING_CLOSURE,
        )
        self.assertTrue(_is_closure_tombstone(sub))

    def test_active_is_not_tombstone(self):
        sub = ListOfSubscriber(
            id="A1",
            code="A1",
            status=ListOfSubscriber.STATUS_ACTIVE,
        )
        self.assertFalse(_is_closure_tombstone(sub))

    def test_delete_preserves_closed_and_pending_missing_from_remote(self):
        ListOfSubscriber.objects.create(
            id="CLOSED1",
            code="CLOSED1",
            status=ListOfSubscriber.STATUS_CLOSED,
            closed_at=timezone.now(),
            closed_reason="test",
        )
        ListOfSubscriber.objects.create(
            id="PEND1",
            code="PEND1",
            status=ListOfSubscriber.STATUS_PENDING_CLOSURE,
        )
        ListOfSubscriber.objects.create(
            id="GONE1",
            code="GONE1",
            status=ListOfSubscriber.STATUS_ACTIVE,
        )
        ListOfSubscriber.objects.create(
            id="KEEP1",
            code="KEEP1",
            status=ListOfSubscriber.STATUS_ACTIVE,
        )

        result = _delete_local_subscribers_not_in_remote(
            {"CLOSED1", "PEND1", "GONE1", "KEEP1"},
            {"KEEP1"},
        )

        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["preserved_closed"], 2)
        self.assertTrue(ListOfSubscriber.objects.filter(code="CLOSED1").exists())
        self.assertTrue(ListOfSubscriber.objects.filter(code="PEND1").exists())
        self.assertTrue(ListOfSubscriber.objects.filter(code="KEEP1").exists())
        self.assertFalse(ListOfSubscriber.objects.filter(code="GONE1").exists())

    def test_update_skips_closed_subscriber(self):
        sub = ListOfSubscriber.objects.create(
            id="CLOSED2",
            code="CLOSED2",
            firstName="Antes",
            status=ListOfSubscriber.STATUS_CLOSED,
            closed_at=timezone.now(),
        )
        changed = _update_subscriber_from_row(
            sub,
            {"firstName": "Despues", "subscriberCode": "CLOSED2"},
        )
        self.assertFalse(changed)
        sub.refresh_from_db()
        self.assertEqual(sub.firstName, "Antes")
        self.assertEqual(sub.status, ListOfSubscriber.STATUS_CLOSED)
