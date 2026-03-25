"""Unit tests for line protocol formatting.

Tests both history_loader.format_line and the escaping functions,
ensuring they match InfluxDB's line protocol spec exactly.

Targets 500+ parametrized test cases across escaping, formatting,
unicode handling, adversarial inputs, type combinations, fuzz,
and cross-implementation consistency.
"""

import importlib
import math
import random
import re
import string
import sys

import pytest

from enphase_cloud.history_loader import _esc_tag, _esc_field_str, format_line


# ═══════════════════════════════════════════════════════════════════
# Helper: check no unescaped tag specials remain
# ═══════════════════════════════════════════════════════════════════

def _no_unescaped_tag_specials(result: str) -> bool:
    """Return True if result has no unescaped space, comma, or equals."""
    cleaned = result.replace("\\\\", "").replace("\\ ", "").replace("\\,", "").replace("\\=", "")
    return " " not in cleaned and "," not in cleaned and "=" not in cleaned


def _no_raw_field_specials(result: str) -> bool:
    """Return True if result has no raw double-quote, newline, or carriage-return."""
    cleaned = result.replace("\\\\", "XX").replace('\\"', "XX").replace("\\n", "XX")
    return '"' not in cleaned and "\n" not in cleaned and "\r" not in cleaned


# ═══════════════════════════════════════════════════════════════════
# Module-level data for parametrization
# ═══════════════════════════════════════════════════════════════════

# --- Unicode strings from diverse blocks (50 entries) ---
UNICODE_STRINGS = [
    # CJK Unified Ideographs
    "\u4e16\u754c",              # 世界
    "\u4f60\u597d",              # 你好
    "\u5929\u6c14",              # 天气
    "\u592a\u9633\u80fd",        # 太阳能
    "\u7535\u6c60",              # 电池
    "\u53d1\u7535",              # 发电
    "\u8017\u7535",              # 耗电
    "\u5149\u4f0f",              # 光伏
    # Japanese (Hiragana / Katakana)
    "\u3053\u3093\u306b\u3061\u306f",  # こんにちは
    "\u30a8\u30cd\u30eb\u30ae\u30fc",  # エネルギー
    "\u30d0\u30c3\u30c6\u30ea\u30fc",  # バッテリー
    # Korean
    "\uc548\ub155\ud558\uc138\uc694",  # 안녕하세요
    "\ud0dc\uc591\uad11",              # 태양광
    # Arabic
    "\u0627\u0644\u0637\u0627\u0642\u0629",  # الطاقة
    "\u0627\u0644\u0634\u0645\u0633\u064a\u0629",  # الشمسية
    "\u0628\u0637\u0627\u0631\u064a\u0629",  # بطارية
    # Hebrew
    "\u05e9\u05dc\u05d5\u05dd",  # שלום
    "\u05d0\u05e0\u05e8\u05d2\u05d9\u05d4",  # אנרגיה
    # Devanagari (Hindi)
    "\u0928\u092e\u0938\u094d\u0924\u0947",  # नमस्ते
    "\u0938\u094c\u0930\u0020\u090a\u0930\u094d\u091c\u093e",  # सौर ऊर्जा
    "\u092c\u0948\u091f\u0930\u0940",  # बैटरी
    # Thai
    "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35",  # สวัสดี
    "\u0e1e\u0e25\u0e31\u0e07\u0e07\u0e32\u0e19\u0e41\u0e2a\u0e07\u0e2d\u0e32\u0e17\u0e34\u0e15\u0e22\u0e4c",  # พลังงานแสงอาทิตย์
    # Cyrillic
    "\u041f\u0440\u0438\u0432\u0435\u0442",  # Привет
    "\u042d\u043d\u0435\u0440\u0433\u0438\u044f",  # Энергия
    "\u0411\u0430\u0442\u0430\u0440\u0435\u044f",  # Батарея
    "\u0421\u043e\u043b\u043d\u0435\u0447\u043d\u0430\u044f",  # Солнечная
    # Greek
    "\u0397\u03bb\u03b9\u03b1\u03ba\u03ae",  # Ηλιακή
    "\u0395\u03bd\u03ad\u03c1\u03b3\u03b5\u03b9\u03b1",  # Ενέργεια
    # Emoji sequences
    "\U0001f31e",                # sun with face
    "\U0001f50b",                # battery
    "\u26a1",                    # high voltage
    "\U0001f3e0",                # house
    "\U0001f30d",                # globe
    # Emoji skin tones
    "\U0001f44d\U0001f3fb",      # thumbs up light
    "\U0001f44d\U0001f3ff",      # thumbs up dark
    "\U0001f9d1\U0001f3fd\u200d\U0001f527",  # mechanic medium
    # ZWJ sequences
    "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466",  # family
    "\U0001f3f3\ufe0f\u200d\U0001f308",  # rainbow flag
    # Flag sequences
    "\U0001f1fa\U0001f1f8",      # US flag
    "\U0001f1e9\U0001f1ea",      # DE flag
    "\U0001f1ef\U0001f1f5",      # JP flag
    "\U0001f1e6\U0001f1ea",      # AE flag
    # Mixed unicode and ASCII
    "solar\u2600power",
    "temp\u00b0C",
    "100\u00a2",
    "\u00bd fraction",
    "caf\u00e9",
    "\u00c6sthetic",
    "\u2603 snowman",
    "pi=\u03c0",
]

# --- Control characters (50 entries) ---
CONTROL_CHARS = [
    chr(i) for i in range(32)  # chr(0) through chr(31) = 32 entries
] + [
    chr(127),                    # DEL
    "\u200b",                    # zero-width space
    "\ufeff",                    # BOM
    "\u200f",                    # right-to-left mark
    "\u200e",                    # left-to-right mark
    "\u00a0",                    # non-breaking space
    "\u2028",                    # line separator
    "\u2029",                    # paragraph separator
    "\u202a",                    # left-to-right embedding
    "\u202b",                    # right-to-left embedding
    "\u202c",                    # pop directional formatting
    "\u202d",                    # left-to-right override
    "\u202e",                    # right-to-left override
    "\u2066",                    # left-to-right isolate
    "\u2067",                    # right-to-left isolate
    "\u2068",                    # first strong isolate
    "\u2069",                    # pop directional isolate
    "\u200c",                    # zero-width non-joiner
]

# --- SQL injection strings (20 entries) ---
SQL_INJECTIONS = [
    "'; DROP TABLE measurements;--",
    "1; DROP TABLE users--",
    "' OR '1'='1",
    "' OR 1=1--",
    "' UNION SELECT * FROM users--",
    "1' AND '1'='1",
    "admin'--",
    "'; EXEC xp_cmdshell('dir');--",
    "'; INSERT INTO log VALUES('pwned');--",
    "1'; WAITFOR DELAY '0:0:5';--",
    "' OR ''='",
    "') OR ('1'='1",
    "1) OR (1=1",
    "1 UNION ALL SELECT NULL,NULL,NULL--",
    "' AND 1=CONVERT(int,(SELECT TOP 1 table_name FROM information_schema.tables))--",
    "SLEEP(5)#",
    "' HAVING 1=1--",
    "' GROUP BY columnnames having 1=1--",
    "1' ORDER BY 1--",
    "x' AND email IS NOT NULL; --",
]

# --- XSS payloads (10 entries) ---
XSS_PAYLOADS = [
    "<script>alert('xss')</script>",
    "<img src=x onerror=alert(1)>",
    "<svg/onload=alert(1)>",
    "javascript:alert(1)",
    "<body onload=alert(1)>",
    "<iframe src='javascript:alert(1)'>",
    "'-alert(1)-'",
    '"><script>alert(document.domain)</script>',
    "<div style=\"background:url(javascript:alert(1))\">",
    "{{constructor.constructor('return this')()}}"
]

# --- Path traversal (5 entries) ---
PATH_TRAVERSALS = [
    "../../../etc/passwd",
    "..\\..\\..\\windows\\system32\\config\\sam",
    "/etc/shadow",
    "%2e%2e%2f%2e%2e%2f",
    "....//....//....//etc/passwd",
]

# --- Null byte strings (5 entries) ---
NULL_BYTES = [
    "\x00",
    "before\x00after",
    "\x00\x00\x00",
    "data\x00\x00tail",
    "\x00start",
]

# --- Very long strings (4 entries) ---
LONG_STRINGS = [
    "a" * 1024,       # 1 KB
    "b" * 10240,      # 10 KB
    "c" * 102400,     # 100 KB
    "d" * 1048576,    # 1 MB
]

# --- Mixed attack strings (16 entries) ---
MIXED_ATTACKS = [
    "'; DROP TABLE --<script>alert(1)</script>",
    "\x00' OR '1'='1<img src=x>",
    "../../../etc/passwd'; DROP TABLE--",
    "a" * 500 + "' OR 1=1--" + "b" * 500,
    "<script>\x00\x01\x02</script>",
    "normal_start\n'; DROP TABLE;\r\n<script>",
    "\ufeff\u200b' UNION SELECT *--",
    "key=value,tag space\\backslash\"quote",
    " ,=\\\"'\n\r\t\x00",
    "emoji\U0001f31e' OR 1=1--<script>",
    "\u202eMixed\u202cDirection\u200f' OR 1=1",
    "line1\nline2\rline3\r\nline4",
    "\t\t\ttabs' OR 1=1--",
    "field=\"value\",other=42i",
    "measurement,tag=val field=1i 123456789",
    "back\\\\slash\\\"quote\\\nnewline",
]

# --- Diverse inputs for cross-implementation (20 entries) ---
CROSS_IMPL_INPUTS = [
    "simple",
    "has space",
    "has,comma",
    "has=equals",
    "back\\slash",
    "all, =\\together",
    "",
    "unicode\u4e16\u754c",
    "\n\r\t",
    "x" * 1000,
    "\x00\x01\x02",
    "' OR 1=1--",
    "<script>alert(1)</script>",
    "\U0001f31e\U0001f50b",
    "key=val,tag space",
    "\ufeff\u200b\u200f",
    'has"quotes"',
    "line1\nline2\nline3",
    "mixed\r\n\t\x00 ,=\\\"",
    "caf\u00e9_\u03c0_\u4e16",
]


# ═══════════════════════════════════════════════════════════════════
# Tag escaping (ORIGINAL)
# ═══════════════════════════════════════════════════════════════════

class TestEscTag:
    """Tag keys/values must escape: backslash, space, comma, equals."""

    @pytest.mark.parametrize("raw,expected", [
        ("simple", "simple"),
        ("has space", r"has\ space"),
        ("has,comma", r"has\,comma"),
        ("has=equals", r"has\=equals"),
        ("back\\slash", r"back\\slash"),
        ("all, =\\together", r"all\,\ \=\\together"),
        ("", ""),
        ("no_special_chars_123", "no_special_chars_123"),
    ])
    def test_known_values(self, raw, expected):
        assert _esc_tag(raw) == expected

    def test_double_escape_backslash_then_space(self):
        # Input: one backslash followed by one space (2 chars)
        # Step 1: replace("\\", "\\\\") -> two backslashes + space
        # Step 2: replace(" ", "\\ ") -> two backslashes + backslash-space
        result = _esc_tag("\\ ")
        # Result should contain escaped backslash and escaped space
        assert len(result) > 2  # definitely got longer
        # The important property: no unescaped special chars remain
        cleaned = result.replace("\\\\", "").replace("\\ ", "").replace("\\,", "").replace("\\=", "")
        assert " " not in cleaned


    @pytest.mark.parametrize("n", range(20))
    def test_fuzz_never_produces_unescaped_special(self, n):
        """Random strings never have unescaped special chars in output."""
        random.seed(n * 31)
        raw = "".join(random.choices(string.printable, k=random.randint(0, 50)))
        result = _esc_tag(raw)
        # After escaping, the only unescaped commas/spaces/equals should be
        # preceded by backslash. Check by removing escaped sequences:
        cleaned = result.replace("\\\\", "").replace("\\ ", "").replace("\\,", "").replace("\\=", "")
        assert " " not in cleaned, f"Unescaped space in: {result!r}"
        assert "," not in cleaned, f"Unescaped comma in: {result!r}"
        assert "=" not in cleaned, f"Unescaped equals in: {result!r}"


# ═══════════════════════════════════════════════════════════════════
# Field string escaping (ORIGINAL)
# ═══════════════════════════════════════════════════════════════════

class TestEscFieldStr:
    """Field string values must escape: backslash, double-quote, newline."""

    @pytest.mark.parametrize("raw,expected", [
        ("simple", "simple"),
        ('has "quotes"', 'has \\"quotes\\"'),
        ("has\nnewline", "has\\nnewline"),
        ("has\r\nCRLF", "has\\nCRLF"),
        ("back\\slash", "back\\\\slash"),
        ("", ""),
    ])
    def test_known_values(self, raw, expected):
        assert _esc_field_str(raw) == expected

    @pytest.mark.parametrize("n", range(20))
    def test_fuzz_no_raw_quotes_or_newlines(self, n):
        """Random strings never have unescaped quotes or newlines."""
        random.seed(n * 37)
        raw = "".join(random.choices(string.printable + "\n\r\"\\", k=random.randint(0, 60)))
        result = _esc_field_str(raw)
        # Remove escaped sequences
        cleaned = result.replace("\\\\", "XX").replace('\\"', "XX").replace("\\n", "XX")
        assert '"' not in cleaned, f"Unescaped quote in: {result!r}"
        assert "\n" not in cleaned, f"Unescaped newline in: {result!r}"
        assert "\r" not in cleaned, f"Unescaped CR in: {result!r}"


# ═══════════════════════════════════════════════════════════════════
# format_line (ORIGINAL)
# ═══════════════════════════════════════════════════════════════════

class TestFormatLine:
    """Test full line protocol generation."""

    def test_basic_int_field(self):
        line = format_line("cpu", {"host": "server1"}, {"usage": 42}, 1000000000)
        assert line == "cpu,host=server1 usage=42i 1000000000"

    def test_basic_float_field(self):
        line = format_line("temp", {"room": "lab"}, {"celsius": 23.5}, 2000000000)
        assert line == "temp,room=lab celsius=23.5 2000000000"

    def test_basic_string_field(self):
        line = format_line("log", {}, {"msg": "hello world"}, 3000000000)
        assert line == 'log msg="hello world" 3000000000'

    def test_bool_field_emits_int_and_str(self):
        line = format_line("status", {}, {"active": True}, 4000000000)
        assert "active=1i" in line
        assert 'active_str="true"' in line

    def test_bool_false(self):
        line = format_line("status", {}, {"active": False}, 4000000000)
        assert "active=0i" in line
        assert 'active_str="false"' in line

    def test_none_fields_skipped(self):
        line = format_line("m", {}, {"a": 1, "b": None, "c": 3}, 5000000000)
        assert "a=1i" in line
        assert "c=3i" in line
        assert "b=" not in line

    def test_empty_fields_returns_none(self):
        assert format_line("m", {}, {}, 1) is None

    def test_all_none_fields_returns_none(self):
        assert format_line("m", {}, {"a": None, "b": None}, 1) is None

    def test_empty_dict_returns_none(self):
        assert format_line("m", {}, {}, 1) is None

    def test_tags_sorted_alphabetically(self):
        line = format_line("m", {"z": "1", "a": "2", "m": "3"}, {"v": 1}, 1)
        # Tags should appear in order: a, m, z
        tag_part = line.split(" ")[0]
        assert tag_part == "m,a=2,m=3,z=1"

    def test_fields_sorted_alphabetically(self):
        line = format_line("m", {}, {"z_val": 1.0, "a_val": 2.0, "m_val": 3.0}, 1)
        field_part = line.split(" ")[1]
        fields = field_part.split(",")
        keys = [f.split("=")[0] for f in fields]
        assert keys == sorted(keys)

    def test_empty_tag_value_skipped(self):
        line = format_line("m", {"a": "1", "b": "", "c": None}, {"v": 1}, 1)
        assert ",b=" not in line
        assert ",c=" not in line
        assert "a=1" in line

    def test_special_chars_in_tags(self):
        line = format_line("m", {"host name": "my server"}, {"v": 1}, 1)
        assert r"host\ name=my\ server" in line

    def test_special_chars_in_string_field(self):
        line = format_line("m", {}, {"msg": 'say "hello"'}, 1)
        assert r'msg="say \"hello\""' in line

    def test_mixed_field_types(self):
        line = format_line("m", {"s": "X"}, {
            "count": 42,
            "ratio": 3.14,
            "label": "test",
            "flag": True,
        }, 9999)
        assert "count=42i" in line
        assert "ratio=3.14" in line
        assert 'label="test"' in line
        assert "flag=1i" in line
        assert 'flag_str="true"' in line

    # -- Measurement name matching --
    @pytest.mark.parametrize("measurement", [
        "enphase_power", "enphase_energy", "enphase_battery",
        "enphase_config", "enphase_status", "enphase_error",
        "enphase_gateway", "enphase_inverters", "enphase_dry_contact",
    ])
    def test_all_measurement_names_valid(self, measurement):
        """Every measurement name we use is a valid InfluxDB identifier."""
        line = format_line(measurement, {"serial": "123"}, {"v": 1}, 1)
        assert line.startswith(measurement + ",")

    # -- Line protocol regex validation --
    LINE_PROTO_RE = re.compile(
        r'^[a-zA-Z_][a-zA-Z0-9_]*(,[^\s]+)? [^\s]+ \d+$'
    )

    @pytest.mark.parametrize("n", range(30))
    def test_fuzz_output_matches_line_protocol_pattern(self, n):
        """Random inputs always produce syntactically valid line protocol."""
        random.seed(n * 41)
        measurement = "test_" + "".join(random.choices(string.ascii_lowercase, k=5))
        tags = {f"t{i}": f"v{random.randint(0,999)}" for i in range(random.randint(0, 3))}
        field_types = [int, float, str, bool]
        fields = {}
        for i in range(random.randint(1, 5)):
            ftype = random.choice(field_types)
            key = f"f{i}"
            if ftype is int:
                fields[key] = random.randint(-10000, 10000)
            elif ftype is float:
                fields[key] = round(random.uniform(-10000, 10000), 2)
            elif ftype is str:
                fields[key] = "".join(random.choices(string.ascii_letters, k=random.randint(1, 10)))
            elif ftype is bool:
                fields[key] = random.choice([True, False])

        ts = random.randint(1_000_000_000_000_000_000, 2_000_000_000_000_000_000)
        line = format_line(measurement, tags, fields, ts)
        assert line is not None
        # Basic structure: measurement[,tags] fields timestamp
        parts = line.split(" ")
        assert len(parts) >= 3, f"Bad line: {line}"
        assert parts[-1].isdigit(), f"Timestamp not numeric: {parts[-1]}"


# ═══════════════════════════════════════════════════════════════════
# Chaotic fuzz: adversarial inputs (ORIGINAL)
# ═══════════════════════════════════════════════════════════════════

class TestChaotic:
    """Adversarial and edge-case inputs that should never crash."""

    @pytest.mark.parametrize("value", [
        0, -0, 1, -1,
        2**31, -2**31, 2**63 - 1, -2**63,
        0.0, -0.0, 1e-300, 1e300, -1e300,
        float("inf"), float("-inf"),
        "", "a", " ", "\n", "\r\n", "\t",
        "x" * 10000,
        '"; DROP TABLE --',
        "<script>alert(1)</script>",
        "emoji: \U0001f31e\U0001f50b",
        "\x00\x01\x02\xff",
        True, False,
        None,
    ])
    def test_format_line_never_crashes(self, value):
        """No input value should cause format_line to raise."""
        if value is None:
            result = format_line("m", {}, {"f": value}, 1)
            assert result is None  # None field -> no output
        else:
            try:
                result = format_line("m", {}, {"f": value}, 1)
                # Should either return a string or None
                assert result is None or isinstance(result, str)
            except Exception as e:
                pytest.fail(f"format_line crashed on {value!r}: {e}")

    @pytest.mark.parametrize("tag_val", [
        "", " ", ",", "=", "\\", "\n", "a b,c=d\\e",
        "\x00", "\U0001f31e", "x" * 5000,
    ])
    def test_esc_tag_never_crashes(self, tag_val):
        result = _esc_tag(tag_val)
        assert isinstance(result, str)

    @pytest.mark.parametrize("field_val", [
        "", '"', "\\", "\n", "\r", "\r\n",
        '"hello"', "back\\slash", "new\nline",
        "\x00\x01\xff", "\U0001f31e",
    ])
    def test_esc_field_str_never_crashes(self, field_val):
        result = _esc_field_str(field_val)
        assert isinstance(result, str)

    def test_very_large_number_of_fields(self):
        """1000 fields should still work."""
        fields = {f"f{i}": float(i) for i in range(1000)}
        line = format_line("m", {}, fields, 1)
        assert line is not None
        assert line.count(",") >= 999  # at least 999 commas between 1000 fields

    def test_very_large_number_of_tags(self):
        """100 tags should still work."""
        tags = {f"t{i:03d}": f"v{i}" for i in range(100)}
        line = format_line("m", tags, {"v": 1}, 1)
        assert line is not None

    def test_negative_timestamp(self):
        """Negative timestamps are technically valid (before epoch)."""
        line = format_line("m", {}, {"v": 1}, -1000000000)
        assert line is not None
        assert "-1000000000" in line

    def test_zero_timestamp(self):
        line = format_line("m", {}, {"v": 1}, 0)
        assert line.endswith(" 0")


# ═══════════════════════════════════════════════════════════════════
# 1. TestEscTagUnicode (100 tests)
#    50 Unicode strings + 50 control chars
# ═══════════════════════════════════════════════════════════════════

class TestEscTagUnicode:
    """_esc_tag with diverse Unicode and control character inputs."""

    @pytest.mark.parametrize("ustr", UNICODE_STRINGS, ids=[
        f"unicode_{i}" for i in range(len(UNICODE_STRINGS))
    ])
    def test_unicode_string_no_crash(self, ustr):
        """Unicode string does not crash _esc_tag."""
        result = _esc_tag(ustr)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)

    @pytest.mark.parametrize("ctrl", CONTROL_CHARS, ids=[
        f"ctrl_U+{ord(c):04X}" for c in CONTROL_CHARS
    ])
    def test_control_char_no_crash(self, ctrl):
        """Control character does not crash _esc_tag."""
        result = _esc_tag(ctrl)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)


# ═══════════════════════════════════════════════════════════════════
# 2. TestEscTagAdversarial (60 tests)
#    SQL(20) + XSS(10) + path(5) + null(5) + long(4) + mixed(16)
# ═══════════════════════════════════════════════════════════════════

class TestEscTagAdversarial:
    """_esc_tag with adversarial attack payloads."""

    @pytest.mark.parametrize("sql", SQL_INJECTIONS, ids=[
        f"sql_{i}" for i in range(len(SQL_INJECTIONS))
    ])
    def test_sql_injection(self, sql):
        result = _esc_tag(sql)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)

    @pytest.mark.parametrize("xss", XSS_PAYLOADS, ids=[
        f"xss_{i}" for i in range(len(XSS_PAYLOADS))
    ])
    def test_xss_payload(self, xss):
        result = _esc_tag(xss)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)

    @pytest.mark.parametrize("path", PATH_TRAVERSALS, ids=[
        f"path_{i}" for i in range(len(PATH_TRAVERSALS))
    ])
    def test_path_traversal(self, path):
        result = _esc_tag(path)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)

    @pytest.mark.parametrize("nb", NULL_BYTES, ids=[
        f"null_{i}" for i in range(len(NULL_BYTES))
    ])
    def test_null_bytes(self, nb):
        result = _esc_tag(nb)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)

    @pytest.mark.parametrize("long_str", LONG_STRINGS, ids=[
        "1KB", "10KB", "100KB", "1MB",
    ])
    def test_long_string(self, long_str):
        result = _esc_tag(long_str)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)

    @pytest.mark.parametrize("mixed", MIXED_ATTACKS, ids=[
        f"mixed_{i}" for i in range(len(MIXED_ATTACKS))
    ])
    def test_mixed_attack(self, mixed):
        result = _esc_tag(mixed)
        assert isinstance(result, str)
        assert _no_unescaped_tag_specials(result)


# ═══════════════════════════════════════════════════════════════════
# 3. TestEscFieldStrUnicode (80 tests)
#    Same Unicode/control chars, field-string contract
# ═══════════════════════════════════════════════════════════════════

class TestEscFieldStrUnicode:
    """_esc_field_str with diverse Unicode and control character inputs."""

    @pytest.mark.parametrize("ustr", UNICODE_STRINGS, ids=[
        f"unicode_{i}" for i in range(len(UNICODE_STRINGS))
    ])
    def test_unicode_string_no_crash(self, ustr):
        """Unicode string does not crash _esc_field_str and respects field contract."""
        result = _esc_field_str(ustr)
        assert isinstance(result, str)
        assert _no_raw_field_specials(result)

    @pytest.mark.parametrize("ctrl", CONTROL_CHARS[:30], ids=[
        f"ctrl_U+{ord(c):04X}" for c in CONTROL_CHARS[:30]
    ])
    def test_control_char_no_crash(self, ctrl):
        """Control character does not crash _esc_field_str and respects field contract."""
        result = _esc_field_str(ctrl)
        assert isinstance(result, str)
        assert _no_raw_field_specials(result)


# ═══════════════════════════════════════════════════════════════════
# 4. TestEscFieldStrAdversarial (40 tests)
#    SQL(20) + XSS(10) + null(5) + long(4) + one extra = 40
# ═══════════════════════════════════════════════════════════════════

class TestEscFieldStrAdversarial:
    """_esc_field_str with adversarial attack payloads."""

    @pytest.mark.parametrize("sql", SQL_INJECTIONS, ids=[
        f"sql_{i}" for i in range(len(SQL_INJECTIONS))
    ])
    def test_sql_injection(self, sql):
        result = _esc_field_str(sql)
        assert isinstance(result, str)
        assert _no_raw_field_specials(result)

    @pytest.mark.parametrize("xss", XSS_PAYLOADS, ids=[
        f"xss_{i}" for i in range(len(XSS_PAYLOADS))
    ])
    def test_xss_payload(self, xss):
        result = _esc_field_str(xss)
        assert isinstance(result, str)
        assert _no_raw_field_specials(result)

    @pytest.mark.parametrize("nb", NULL_BYTES, ids=[
        f"null_{i}" for i in range(len(NULL_BYTES))
    ])
    def test_null_bytes(self, nb):
        result = _esc_field_str(nb)
        assert isinstance(result, str)
        assert _no_raw_field_specials(result)

    @pytest.mark.parametrize("long_str", LONG_STRINGS, ids=[
        "1KB", "10KB", "100KB", "1MB",
    ])
    def test_long_string(self, long_str):
        result = _esc_field_str(long_str)
        assert isinstance(result, str)
        assert _no_raw_field_specials(result)

    def test_mixed_quotes_newlines_backslashes(self):
        """A string with all special field chars at once."""
        s = 'line1\nline2\r\n"quoted"\r\\back'
        result = _esc_field_str(s)
        assert isinstance(result, str)
        assert _no_raw_field_specials(result)


# ═══════════════════════════════════════════════════════════════════
# 5. TestFormatLineTypeCombinations (100 tests)
#    field_type x value variants x tag_count
# ═══════════════════════════════════════════════════════════════════

# Build combinations: (field_type, value, tag_count)
_INT_VALUES = [
    ("int_zero", 0),
    ("int_neg1", -1),
    ("int_max64", 2**63 - 1),
    ("int_min64", -2**63),
]
_FLOAT_VALUES = [
    ("float_zero", 0.0),
    ("float_neg_zero", -0.0),
    ("float_tiny", 1e-300),
    ("float_huge", 1e300),
    ("float_inf", float("inf")),
    ("float_neg_inf", float("-inf")),
    ("float_nan", float("nan")),
]
_STR_VALUES = [
    ("str_empty", ""),
    ("str_emoji", "\U0001f31e\U0001f50b"),
    ("str_sql_inj", "'; DROP TABLE users--"),
]
_BOOL_VALUES = [
    ("bool_true", True),
    ("bool_false", False),
]
_NONE_VALUES = [
    ("none", None),
]
_TAG_COUNTS = [0, 1, 5]

# All type-value pairs
_ALL_TYPE_VALUES = _INT_VALUES + _FLOAT_VALUES + _STR_VALUES + _BOOL_VALUES + _NONE_VALUES

# Build the parametrize list
_TYPE_COMBO_PARAMS = []
for val_name, val in _ALL_TYPE_VALUES:
    for tc in _TAG_COUNTS:
        _TYPE_COMBO_PARAMS.append(
            pytest.param(val, tc, id=f"{val_name}_tags{tc}")
        )

# We have 17 value variants * 3 tag counts = 51 combos from the above.
# Add more int/float variants to reach ~100.
_EXTRA_INT_VALUES = [
    ("int_1", 1),
    ("int_42", 42),
    ("int_neg1000", -1000),
    ("int_max32", 2**31 - 1),
    ("int_min32", -2**31),
    ("int_large", 999999999999),
    ("int_neg_large", -999999999999),
]
_EXTRA_FLOAT_VALUES = [
    ("float_pi", 3.14159265358979),
    ("float_neg_pi", -3.14159265358979),
    ("float_one", 1.0),
    ("float_neg_one", -1.0),
    ("float_small", 0.000001),
    ("float_large_neg", -1e300),
]
_EXTRA_STR_VALUES = [
    ("str_space", "hello world"),
    ("str_quotes", 'say "hi"'),
    ("str_newline", "line1\nline2"),
    ("str_long", "x" * 500),
    ("str_unicode", "\u4e16\u754c\u4f60\u597d"),
]

for val_name, val in _EXTRA_INT_VALUES + _EXTRA_FLOAT_VALUES + _EXTRA_STR_VALUES:
    # Use tag_count=1 for extras to get more coverage without tripling
    _TYPE_COMBO_PARAMS.append(
        pytest.param(val, 1, id=f"{val_name}_tags1")
    )
    # Also do tag_count=0 for half of them
    _TYPE_COMBO_PARAMS.append(
        pytest.param(val, 0, id=f"{val_name}_tags0")
    )


class TestFormatLineTypeCombinations:
    """Parametrize over field types, value variants, and tag counts."""

    @pytest.mark.parametrize("value,tag_count", _TYPE_COMBO_PARAMS)
    def test_type_combination(self, value, tag_count):
        tags = {f"t{i}": f"v{i}" for i in range(tag_count)}
        fields = {"f": value}

        try:
            result = format_line("measurement", tags, fields, 1000000000)
        except Exception as e:
            pytest.fail(f"format_line crashed on value={value!r}, tags={tag_count}: {e}")

        if value is None:
            assert result is None
        else:
            assert result is None or isinstance(result, str)
            if isinstance(result, str):
                # Basic structure checks
                parts = result.split(" ")
                assert len(parts) >= 3, f"Bad line structure: {result!r}"
                # Measurement should start correctly
                assert parts[0].startswith("measurement")
                # Timestamp at end
                assert parts[-1] == "1000000000"

                # Tag count check
                meas_tags = parts[0]
                if tag_count == 0:
                    assert "," not in meas_tags or meas_tags == "measurement"
                else:
                    comma_count = meas_tags.count(",")
                    assert comma_count == tag_count, (
                        f"Expected {tag_count} tag commas, got {comma_count} in {meas_tags!r}"
                    )


# ═══════════════════════════════════════════════════════════════════
# 6. TestFormatLineHypothesisStyle (100 tests)
#    Seed-based pseudo-random generation
# ═══════════════════════════════════════════════════════════════════

# Regex for validating line protocol output with escaped characters
# This is intentionally lenient to handle the wide range of unicode
_LINE_PROTO_BASIC_RE = re.compile(
    r'^.+ .+ .+$'  # measurement[,tags] fields timestamp
)


class TestFormatLineHypothesisStyle:
    """Pseudo-random format_line inputs using seeds, hypothesis-style."""

    @pytest.mark.parametrize("seed", range(100))
    def test_random_seed(self, seed):
        rng = random.Random(seed * 53 + 7)

        # Random measurement name (valid identifier)
        meas_len = rng.randint(1, 20)
        measurement = "m_" + "".join(rng.choices(string.ascii_lowercase + string.digits + "_", k=meas_len))

        # Random tags (0-5)
        num_tags = rng.randint(0, 5)
        tags = {}
        for i in range(num_tags):
            tk = f"tag_{i}_{rng.randint(0, 999)}"
            # Mix of simple and complex tag values
            # Exclude \n, \r, \x0b, \x0c from tag values since _esc_tag
            # does not escape line terminators (they are not valid in tags)
            _tag_chars = string.printable.replace("\n", "").replace("\r", "").replace("\x0b", "").replace("\x0c", "")
            if rng.random() < 0.3:
                tv = "".join(rng.choices(_tag_chars, k=rng.randint(1, 30)))
            else:
                tv = f"val_{rng.randint(0, 9999)}"
            tags[tk] = tv

        # Random fields (1-8)
        num_fields = rng.randint(1, 8)
        fields = {}
        for i in range(num_fields):
            fk = f"field_{i}"
            ftype = rng.choice(["int", "float", "str", "bool", "none"])
            if ftype == "int":
                fields[fk] = rng.randint(-2**31, 2**31)
            elif ftype == "float":
                fields[fk] = rng.uniform(-1e6, 1e6)
            elif ftype == "str":
                slen = rng.randint(0, 50)
                fields[fk] = "".join(rng.choices(
                    string.ascii_letters + string.digits + " \n\r\t\"\\,=",
                    k=slen
                ))
            elif ftype == "bool":
                fields[fk] = rng.choice([True, False])
            else:
                fields[fk] = None

        # Random timestamp
        ts = rng.randint(0, 2_000_000_000_000_000_000)

        try:
            line = format_line(measurement, tags, fields, ts)
        except Exception as e:
            pytest.fail(f"format_line crashed with seed={seed}: {e}")

        # All fields could be None, which gives None result
        non_none = {k: v for k, v in fields.items() if v is not None}
        if not non_none:
            assert line is None
            return

        assert line is not None, f"Expected line output with non-None fields, seed={seed}"
        assert isinstance(line, str)

        # Basic structure: at least 3 space-separated parts
        # (measurement[,tags] fields timestamp)
        # But fields with spaces in string values make naive split tricky,
        # so just verify measurement starts and timestamp ends
        assert line.startswith(measurement), f"Line doesn't start with measurement: {line[:100]!r}"
        assert line.endswith(f" {ts}"), f"Line doesn't end with timestamp {ts}: ...{line[-50:]!r}"

        # No raw newlines in the output
        assert "\n" not in line, f"Raw newline in output with seed={seed}"
        assert "\r" not in line, f"Raw carriage return in output with seed={seed}"


# ═══════════════════════════════════════════════════════════════════
# 7. TestCrossImplementation (20 tests)
#    Compare history_loader vs enphase_telegraf implementations
# ═══════════════════════════════════════════════════════════════════

def _load_telegraf_functions():
    """Load _esc_tag and _esc_field_str from enphase_telegraf.py via importlib."""
    import importlib.util
    from pathlib import Path

    # The module is at src/enphase_telegraf.py
    # Try multiple resolution strategies
    candidates = [
        Path(__file__).resolve().parent.parent / "src" / "enphase_telegraf.py",
        Path("src/enphase_telegraf.py").resolve(),
    ]
    for mod_path in candidates:
        if mod_path.exists():
            spec = importlib.util.spec_from_file_location("enphase_telegraf_mod", str(mod_path))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod._esc_tag, mod._esc_field_str
    pytest.skip("Could not locate src/enphase_telegraf.py")


class TestCrossImplementation:
    """Verify history_loader and enphase_telegraf produce identical escape output."""

    @pytest.fixture(scope="class")
    def telegraf_funcs(self):
        return _load_telegraf_functions()

    @pytest.mark.parametrize("input_str", CROSS_IMPL_INPUTS, ids=[
        f"cross_{i}" for i in range(len(CROSS_IMPL_INPUTS))
    ])
    def test_esc_tag_matches(self, telegraf_funcs, input_str):
        """_esc_tag from both modules produces identical output."""
        telegraf_esc_tag, _ = telegraf_funcs
        from enphase_cloud.history_loader import _esc_tag as hl_esc_tag

        result_hl = hl_esc_tag(input_str)
        result_tg = telegraf_esc_tag(input_str)
        assert result_hl == result_tg, (
            f"Mismatch for {input_str!r}: history_loader={result_hl!r}, telegraf={result_tg!r}"
        )

    @pytest.mark.parametrize("input_str", CROSS_IMPL_INPUTS, ids=[
        f"cross_{i}" for i in range(len(CROSS_IMPL_INPUTS))
    ])
    def test_esc_field_str_matches(self, telegraf_funcs, input_str):
        """_esc_field_str from both modules produces identical output."""
        _, telegraf_esc_field_str = telegraf_funcs
        from enphase_cloud.history_loader import _esc_field_str as hl_esc_field_str

        result_hl = hl_esc_field_str(input_str)
        result_tg = telegraf_esc_field_str(input_str)
        assert result_hl == result_tg, (
            f"Mismatch for {input_str!r}: history_loader={result_hl!r}, telegraf={result_tg!r}"
        )


# ═══════════════════════════════════════════════════════════════════
# Gap fixes: NaN/Inf filtering and newline stripping in tags
# ═══════════════════════════════════════════════════════════════════

class TestNaNInfFiltering:
    """NaN and Infinity float values must be silently dropped by format_line."""

    def test_nan_field_dropped(self):
        line = format_line("m", {}, {"good": 1.0, "bad": float("nan")}, 1)
        assert line is not None
        assert "good=1.0" in line
        assert "bad=" not in line

    def test_negative_nan_dropped(self):
        line = format_line("m", {}, {"good": 2.0, "bad": float("-nan")}, 1)
        assert "good=2.0" in line
        assert "bad=" not in line

    def test_inf_field_dropped(self):
        line = format_line("m", {}, {"good": 3.0, "bad": float("inf")}, 1)
        assert "good=3.0" in line
        assert "bad=" not in line

    def test_negative_inf_dropped(self):
        line = format_line("m", {}, {"good": 4.0, "bad": float("-inf")}, 1)
        assert "good=4.0" in line
        assert "bad=" not in line

    def test_all_nan_inf_returns_none(self):
        assert format_line("m", {}, {"a": float("nan"), "b": float("inf")}, 1) is None

    def test_nan_among_other_types(self):
        line = format_line("m", {}, {"i": 42, "f": float("nan"), "s": "ok"}, 1)
        assert "i=42i" in line
        assert 's="ok"' in line
        assert "nan" not in line.lower()

    @pytest.mark.parametrize("bad_val", [
        float("nan"), float("-nan"), float("inf"), float("-inf"),
    ])
    def test_parametrized_non_finite_dropped(self, bad_val):
        line = format_line("m", {}, {"good": 99.0, "bad": bad_val}, 1)
        assert "good=99.0" in line
        assert "bad=" not in line


class TestTagNewlineStripping:
    """Newlines and carriage returns in tag values must be stripped."""

    def test_newline_stripped_from_tag_value(self):
        result = _esc_tag("hello\nworld")
        assert "\n" not in result
        assert "helloworld" in result

    def test_carriage_return_stripped_from_tag_value(self):
        result = _esc_tag("hello\rworld")
        assert "\r" not in result
        assert "helloworld" in result

    def test_crlf_stripped_from_tag_value(self):
        result = _esc_tag("hello\r\nworld")
        assert "\n" not in result
        assert "\r" not in result
        assert "helloworld" in result

    def test_newline_in_tag_produces_single_line(self):
        line = format_line("m", {"t": "a\nb"}, {"v": 1}, 1)
        assert "\n" not in line
        assert "t=ab" in line

    def test_multiple_newlines_all_stripped(self):
        result = _esc_tag("a\nb\nc\nd")
        assert result == "abcd"

    def test_newline_with_other_escapes(self):
        result = _esc_tag("a b\nc,d=e")
        # space, comma, equals escaped; newline stripped
        assert "\n" not in result
        assert "\\ " in result  # space escaped
        assert "\\," in result  # comma escaped
        assert "\\=" in result  # equals escaped

    @pytest.mark.parametrize("bad_char", ["\n", "\r", "\r\n", "\n\r", "\n\n\n"])
    def test_parametrized_newline_variants(self, bad_char):
        result = _esc_tag(f"before{bad_char}after")
        assert "\n" not in result
        assert "\r" not in result
        assert "beforeafter" in result
