from zeitgeist.gdelt.parser import parse_event_row


def make_row(overrides: dict[int, str] | None = None) -> str:
    fields = [""] * 61
    fields[0] = "1234567890"
    fields[1] = "20260818"
    fields[5] = "USAGOV"
    fields[6] = "UNITED STATES"
    fields[15] = "ECB"
    fields[16] = "EUROPEAN CENTRAL BANK"
    fields[26] = "042"
    fields[28] = "04"
    fields[29] = "1"
    fields[30] = "1.9"
    fields[31] = "12"
    fields[34] = "2.5"
    fields[52] = "Frankfurt, Hessen, Germany"
    fields[56] = "50.11"
    fields[57] = "8.68"
    fields[59] = "20260818143000"
    fields[60] = "https://example.com/article"
    for index, value in (overrides or {}).items():
        fields[index] = value
    return "\t".join(fields)


def test_parses_full_row():
    ev = parse_event_row(make_row())
    assert ev is not None
    assert ev.event_id == "1234567890"
    assert ev.occurred_on == "2026-08-18"
    assert ev.actor1_name == "UNITED STATES"
    assert ev.actor2_name == "EUROPEAN CENTRAL BANK"
    assert ev.event_code == "042"
    assert ev.event_root_code == "04"
    assert ev.quad_class == 1
    assert ev.goldstein == 1.9
    assert ev.num_mentions == 12
    assert ev.avg_tone == 2.5
    assert ev.geo_name == "Frankfurt, Hessen, Germany"
    assert ev.geo_lat == 50.11
    assert ev.geo_lon == 8.68
    assert ev.observed_at == "2026-08-18T14:30:00Z"
    assert ev.source_url == "https://example.com/article"


def test_empty_optional_fields_become_none():
    ev = parse_event_row(make_row({15: "", 16: "", 30: "", 34: "", 52: "", 56: "", 57: "", 60: ""}))
    assert ev is not None
    assert ev.actor2_name is None
    assert ev.goldstein is None
    assert ev.geo_lat is None
    assert ev.source_url is None


def test_wrong_column_count_returns_none():
    assert parse_event_row("only\tthree\tcolumns") is None


def test_unparseable_numeric_returns_none():
    assert parse_event_row(make_row({31: "not-a-number"})) is None


def test_bad_timestamp_returns_none():
    assert parse_event_row(make_row({59: "garbage"})) is None
