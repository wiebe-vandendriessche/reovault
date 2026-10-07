"""The Footage calendar's month navigation."""

import re


def test_month_buttons_navigate_and_target_their_own_calendar(env, auth_client):
    r = auth_client.get("/fragments/calendar?month=2026-10")
    assert r.status_code == 200
    assert "October 2026" in r.text

    # recordings.html loads this fragment twice (mobile + desktop): an id
    # target would always swap the first, hidden copy, so the visible
    # calendar never moved off the current month.
    assert 'id="calendar"' not in r.text
    assert r.text.count('hx-target="closest .calendar-panel"') == 2

    prev_url = re.search(r'hx-get="([^"]*month=2026-09[^"]*)"', r.text)
    assert prev_url is not None
    assert "September 2026" in auth_client.get(prev_url.group(1).replace("&amp;", "&")).text
