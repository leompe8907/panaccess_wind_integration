from django.test import TestCase, SimpleTestCase
from unittest.mock import patch, MagicMock

from wind.services.subscriber_trial import is_eligible_for_trial, mark_trial_granted
from wind.models import SubscriberEmailRegistry, SubscriberDocumentRegistry


class SubscriberTrialEligibilityTestCase(SimpleTestCase):
    def test_new_email_is_eligible(self):
        self.assertTrue(is_eligible_for_trial(email="new@example.com"))

    @patch("wind.services.subscriber_trial.SubscriberEmailRegistry.objects.filter")
    def test_closed_account_not_eligible_for_trial(self, mock_filter):
        reg = MagicMock()
        reg.has_purchased = False
        reg.account_closed_at = "2025-01-01"
        reg.trial_used = True
        reg.eligible_for_trial = False
        mock_filter.return_value.first.return_value = reg
        self.assertFalse(is_eligible_for_trial(email="closed@example.com"))

    @patch("wind.services.subscriber_trial.SubscriberEmailRegistry.objects.filter")
    def test_active_trial_used_not_eligible(self, mock_filter):
        reg = MagicMock()
        reg.has_purchased = False
        reg.account_closed_at = None
        reg.trial_used = True
        reg.eligible_for_trial = False
        mock_filter.return_value.first.return_value = reg
        self.assertFalse(is_eligible_for_trial(email="used@example.com"))


class MarkTrialGrantedTestCase(TestCase):

    def test_mark_trial_granted_sets_flags(self):
        mark_trial_granted(
            email="trial@example.com",
            document="DOC123",
            subscriber_code="SUB001",
        )
        email_reg = SubscriberEmailRegistry.objects.get(email="trial@example.com")
        doc_reg = SubscriberDocumentRegistry.objects.get(document="DOC123")
        self.assertTrue(email_reg.trial_used)
        self.assertFalse(email_reg.eligible_for_trial)
        self.assertTrue(doc_reg.trial_used)
