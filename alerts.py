"""Alert rules. Pure functions only: no database access, easy to test."""
import config


def is_breached(sensor_id, value):
    """True if `value` is beyond the sensor's alert threshold."""
    rule = config.ALERT_RULES.get(sensor_id)
    if rule is None or value is None:
        return False
    if rule["condition"] == ">":
        return value > rule["threshold"]
    return value < rule["threshold"]


def crossed(sensor_id, previous, new):
    """Return {alert_type, message} if `new` just crossed into the breached state, else None.

    No alert is repeated while the sensor stays breached (previous already breached).
    """
    if not is_breached(sensor_id, new) or is_breached(sensor_id, previous):
        return None
    rule = config.ALERT_RULES[sensor_id]
    unit = config.SENSORS[sensor_id]["unit"]
    return {
        "alert_type": rule["alert_type"],
        "message": f"{rule['label']}: {new:.2f} {unit}",
    }
