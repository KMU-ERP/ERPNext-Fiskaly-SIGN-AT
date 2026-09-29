from datetime import datetime
from unittest import TestCase
from unittest.mock import patch

from erpnext_fiskaly_sign_at.providers.errors import PermanentFiskalyError
from erpnext_fiskaly_sign_at.time_utils import provider_datetime_for_db


class TestProviderDatetime(TestCase):
	def setUp(self):
		self.system_timezone = patch(
			"erpnext_fiskaly_sign_at.time_utils.get_system_timezone",
			return_value="Europe/Vienna",
		)
		self.system_timezone.start()
		self.addCleanup(self.system_timezone.stop)

	def test_utc_epoch_is_stored_as_naive_system_local_time(self):
		value = int(datetime.fromisoformat("2026-08-19T07:12:09+00:00").timestamp())

		self.assertEqual(provider_datetime_for_db(value), datetime(2026, 8, 19, 9, 12, 9))

	def test_offset_iso_timestamp_is_stored_as_same_system_local_instant(self):
		self.assertEqual(
			provider_datetime_for_db("2026-08-19T07:12:09+00:00"),
			datetime(2026, 8, 19, 9, 12, 9),
		)
		self.assertEqual(
			provider_datetime_for_db("2026-08-19T09:12:09+02:00"),
			datetime(2026, 8, 19, 9, 12, 9),
		)

	def test_naive_frappe_datetime_remains_unchanged(self):
		self.assertEqual(
			provider_datetime_for_db("2026-08-19 09:12:09"),
			datetime(2026, 8, 19, 9, 12, 9),
		)

	def test_configured_system_timezone_is_used(self):
		with patch(
			"erpnext_fiskaly_sign_at.time_utils.get_system_timezone",
			return_value="America/New_York",
		):
			self.assertEqual(
				provider_datetime_for_db("2026-08-19T07:12:09+00:00"),
				datetime(2026, 8, 19, 3, 12, 9),
			)

	def test_invalid_provider_timestamp_fails_closed(self):
		with self.assertRaises(PermanentFiskalyError) as failure:
			provider_datetime_for_db("not-a-datetime")

		self.assertEqual(failure.exception.code, "E_INVALID_PROVIDER_DATETIME")
