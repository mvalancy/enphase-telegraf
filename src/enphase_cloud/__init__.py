"""Enphase Enlighten cloud API — auth, data, control, and live streaming.

Modules:
  enlighten   — Portal API client (auth + 20 data getters + 6 control methods)
  livestream  — MQTT over WebSocket real-time protobuf stream (~1Hz)
  history     — Historical data trickle-downloader (15-min intervals)

Usage:
    from enphase_cloud.enlighten import EnlightenClient
    from enphase_cloud.livestream import LiveStreamClient

    client = EnlightenClient(email, password)
    client.login()

    # Read data
    data = client.get_site_data()
    battery = client.get_battery_settings()

    # Control
    client.set_battery_mode("self-consumption")
    client.set_reserve_soc(20)

    # Live stream (~1 msg/sec with full system state)
    stream = LiveStreamClient(client)
    stream.start(serial, on_data=lambda d: print(d))

    # History clone (backgrounds, ~2 req/min)
    cloner = HistoryCloner(client, cache_dir, site_id)
    cloner.run(start_date="2024-01-01")
"""

from .enlighten import EnlightenClient, AuthError, MFARequired
