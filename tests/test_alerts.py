import unittest

import alerts


class IsBreachedTests(unittest.TestCase):
    def test_thresholds_are_strict(self):
        cases = [
            ("TEMP-01", 40.0, False), ("TEMP-01", 40.01, True),
            ("HUM-01", 30.0, False), ("HUM-01", 29.99, True),
            ("PRES-01", 990.0, False), ("PRES-01", 989.99, True),
        ]
        for sensor_id, value, expected in cases:
            with self.subTest(sensor_id=sensor_id, value=value):
                self.assertEqual(alerts.is_breached(sensor_id, value), expected)

    def test_each_rule_applies_to_its_own_sensor_only(self):
        self.assertFalse(alerts.is_breached("HUM-01", 45))   # 45 % humidity is fine
        self.assertFalse(alerts.is_breached("TEMP-01", 25))  # 25 °C is fine

    def test_missing_value_or_unknown_sensor_is_not_breached(self):
        self.assertFalse(alerts.is_breached("TEMP-01", None))
        self.assertFalse(alerts.is_breached("XX-99", 1000))


class CrossedTests(unittest.TestCase):
    def test_first_reading_beyond_threshold_raises_alert(self):
        alert = alerts.crossed("TEMP-01", None, 45)
        self.assertEqual(alert["alert_type"], "HIGH_TEMPERATURE")
        self.assertEqual(alert["message"], "High Temperature Alert: 45.00 °C")

    def test_normal_to_breached_raises_alert(self):
        self.assertIsNotNone(alerts.crossed("HUM-01", 50, 25))
        self.assertEqual(alerts.crossed("PRES-01", 1000, 985)["alert_type"], "LOW_PRESSURE")

    def test_staying_breached_does_not_repeat_alert(self):
        self.assertIsNone(alerts.crossed("TEMP-01", 45, 46))

    def test_recovering_does_not_raise_alert(self):
        self.assertIsNone(alerts.crossed("TEMP-01", 45, 30))

    def test_normal_reading_does_not_raise_alert(self):
        self.assertIsNone(alerts.crossed("TEMP-01", 30, 31))


if __name__ == "__main__":
    unittest.main()
