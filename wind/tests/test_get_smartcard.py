from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from wind.functions.getSmartcard import (
    CallListSmartcards,
    _should_run_full_smartcard_by_subscriber,
    _smartcards_changed_since_filters,
    compare_and_update_all_smartcards,
    compare_and_update_smartcards_by_subscribers,
    run_smartcard_sync_for_pipeline,
)


class CompareSmartcardsBySubscriberTestCase(SimpleTestCase):
    @patch("wind.functions.getSmartcard.PanaccessConfig")
    @patch("wind.functions.getSmartcard._process_subscriber_smartcard_sync")
    @patch("wind.functions.getSmartcard.ListOfSmartcards")
    @patch("wind.functions.getSmartcard.ListOfSubscriber")
    def test_updates_existing_and_creates_new_for_subscriber(
        self,
        mock_subscriber_model,
        mock_smartcard_model,
        mock_process,
        mock_config,
    ):
        mock_config.SMARTCARD_SUBSCRIBER_CONCURRENCY = 1
        mock_subscriber_model.objects.exclude.return_value.exclude.return_value.values_list.return_value = [
            "SUB001",
            "SUB002",
        ]
        mock_smartcard_model.objects.count.return_value = 1
        mock_process.side_effect = [
            {"updated": 1, "created": 1, "deleted": 0, "remote_count": 2},
            {"updated": 0, "created": 1, "deleted": 0, "remote_count": 1},
        ]

        result = compare_and_update_smartcards_by_subscribers(limit=100)

        self.assertEqual(result["strategy"], "by_subscriber")
        self.assertEqual(result["subscribers_total"], 2)
        self.assertEqual(result["subscribers_processed"], 2)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["remote_count"], 3)
        self.assertEqual(mock_process.call_count, 2)

    @patch("wind.functions.getSmartcard.PanaccessConfig")
    @patch("wind.functions.getSmartcard.ListOfSmartcards")
    @patch("wind.functions.getSmartcard.ListOfSubscriber")
    @patch("wind.functions.getSmartcard._fetch_smartcards_for_subscriber")
    def test_deletes_local_orphans_for_subscriber(
        self,
        mock_fetch,
        mock_subscriber_model,
        mock_smartcard_model,
        mock_config,
    ):
        mock_config.SMARTCARD_SUBSCRIBER_CONCURRENCY = 1
        mock_subscriber_model.objects.exclude.return_value.exclude.return_value.values_list.return_value = [
            "SUB001"
        ]
        mock_smartcard_model.objects.count.return_value = 2

        existing = MagicMock(sn="111111111111111")
        orphan = MagicMock(sn="999999999999999")

        def smartcard_filter(**kwargs):
            qs = MagicMock()
            if kwargs.get("subscriberCode") == "SUB001":
                if kwargs.get("sn__in"):
                    qs.delete.return_value = (1, {"wind.ListOfSmartcards": 1})
                    return qs
                qs.exclude.return_value.exclude.return_value = [existing, orphan]
                qs.count.return_value = 1
            else:
                qs.first.return_value = None
            return qs

        mock_smartcard_model.objects.filter.side_effect = smartcard_filter
        mock_fetch.return_value = [
            {
                "sn": "111111111111111",
                "subscriberCode": "SUB001",
                "firstName": "Ana",
                "lastName": "Perez",
            }
        ]

        result = compare_and_update_smartcards_by_subscribers(limit=100)

        self.assertEqual(result["deleted"], 1)

    @patch("wind.functions.getSmartcard.PanaccessConfig")
    @patch("wind.functions.getSmartcard._compare_and_update_all_smartcards_full")
    @patch("wind.functions.getSmartcard.compare_and_update_smartcards_by_subscribers")
    def test_routes_to_full_scan_when_forced(self, mock_by_sub, mock_full, mock_config):
        mock_config.SMARTCARD_SYNC_BY_SUBSCRIBER = True
        mock_full.return_value = {"strategy": "full_scan"}

        compare_and_update_all_smartcards(limit=50, force_full=True)

        mock_full.assert_called_once_with(None, 50)
        mock_by_sub.assert_not_called()

    @patch("wind.functions.getSmartcard.get_panaccess")
    def test_call_list_omits_order_when_disabled(self, mock_get_pa):
        client = mock_get_pa.return_value
        client.call.return_value = {"success": True, "answer": {}}

        CallListSmartcards(
            offset=0,
            limit=10,
            subscriber_code="SUB001",
            order_by_sn=False,
        )

        parameters = client.call.call_args[0][1]
        self.assertNotIn("orderBy", parameters)
        self.assertNotIn("orderDir", parameters)
        self.assertEqual(parameters["subscriberCode"], "SUB001")
        self.assertIn("filters", parameters)
        self.assertEqual(parameters["filters"]["rules"][0]["field"], "subscriberCode")

    def test_incremental_filters_or_last_contact_and_activation(self):
        filt = _smartcards_changed_since_filters("2026-06-17 10:00:00")
        self.assertEqual(filt["groupOp"], "OR")
        fields = {r["field"] for r in filt["rules"]}
        self.assertEqual(fields, {"lastContact", "lastActivation"})
        self.assertTrue(all(r["op"] == "gt" for r in filt["rules"]))

    @patch("wind.functions.getSmartcard.RedisConfig")
    @patch("wind.functions.getSmartcard.PanaccessConfig")
    def test_should_run_full_by_subscriber_when_never_ran(
        self, mock_config, mock_redis
    ):
        mock_config.SMARTCARD_SYNC_BY_SUBSCRIBER = True
        mock_config.SMARTCARD_PIPELINE_COMPLETE_EACH_CYCLE = False
        mock_config.SMARTCARD_FULL_BY_SUBSCRIBER_EVERY_HOURS = 24
        mock_redis.get_smartcard_full_by_subscriber_at.return_value = None
        self.assertTrue(_should_run_full_smartcard_by_subscriber())

    @patch("wind.functions.getSmartcard.PanaccessConfig")
    def test_should_run_full_each_cycle_when_complete_flag(self, mock_config):
        mock_config.SMARTCARD_SYNC_BY_SUBSCRIBER = True
        mock_config.SMARTCARD_PIPELINE_COMPLETE_EACH_CYCLE = True
        self.assertTrue(_should_run_full_smartcard_by_subscriber())

    @patch("wind.functions.getSmartcard.compare_and_update_smartcards_incremental")
    @patch("wind.functions.getSmartcard._should_run_full_smartcard_by_subscriber")
    @patch("wind.functions.getSmartcard.PanaccessConfig")
    def test_pipeline_hybrid_runs_incremental_only(
        self, mock_config, mock_should_full, mock_incremental
    ):
        mock_config.SMARTCARD_SYNC_INCREMENTAL = True
        mock_config.SMARTCARD_PIPELINE_COMPLETE_EACH_CYCLE = False
        mock_should_full.return_value = False
        mock_incremental.return_value = {"strategy": "incremental", "remote_count": 2}

        result = run_smartcard_sync_for_pipeline(limit=50)

        self.assertEqual(result["strategy"], "pipeline_hybrid")
        mock_incremental.assert_called_once()
        self.assertIn("incremental", result["steps"])
        self.assertNotIn("by_subscriber", result["steps"])
