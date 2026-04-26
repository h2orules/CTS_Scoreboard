from datetime import date

import pytest

from hytek_rec_parser import (
    EPOCH,
    _format_time,
    _mbf_single_to_float,
    _parse_date,
    _yy_to_yyyy,
    format_record_date,
    parse_rec_file,
)


class TestMbfSingleToFloat:
    def test_zero(self):
        assert _mbf_single_to_float(b"\x00\x00\x00\x00") == 0.0

    def test_one(self):
        result = _mbf_single_to_float(b"\x00\x00\x00\x81")
        assert abs(result - 1.0) < 1e-6

    def test_negative_one(self):
        result = _mbf_single_to_float(b"\x00\x00\x80\x81")
        assert abs(result - (-1.0)) < 1e-6

    def test_wrong_length(self):
        with pytest.raises(ValueError):
            _mbf_single_to_float(b"\x00\x00")


class TestFormatTime:
    def test_sub_minute(self):
        assert _format_time(25.43) == "25.43"

    def test_over_minute(self):
        assert _format_time(65.0) == "1:05.00"

    def test_zero(self):
        assert _format_time(0.0) == ""

    def test_exact_minute(self):
        assert _format_time(60.0) == "1:00.00"

    def test_large_time(self):
        result = _format_time(125.99)
        assert result.startswith("2:")


class TestYyToYyyy:
    def test_2000(self):
        assert _yy_to_yyyy(0) == 2000

    def test_2068(self):
        assert _yy_to_yyyy(68) == 2068

    def test_1969(self):
        assert _yy_to_yyyy(69) == 1969

    def test_1999(self):
        assert _yy_to_yyyy(99) == 1999


class TestParseDate:
    def test_full_date(self):
        assert _parse_date("071424") == date(2024, 7, 14)

    def test_blank(self):
        assert _parse_date("      ") == EPOCH

    def test_year_only(self):
        assert _parse_date("    19") == date(2019, 1, 1)

    def test_month_year(self):
        assert _parse_date("07  24") == date(2024, 7, 1)

    def test_short_string(self):
        assert _parse_date("") == EPOCH


class TestFormatRecordDate:
    def test_epoch(self):
        assert format_record_date(EPOCH) == ""

    def test_year_only(self):
        assert format_record_date(date(2019, 1, 1)) == "2019"

    def test_month_year(self):
        assert format_record_date(date(2024, 7, 1)) == "July 2024"

    def test_full_date(self):
        assert format_record_date(date(2024, 7, 14)) == "07/14/2024"


class TestParseRecFile:
    def test_parse_header(self, hytek_dir):
        rec = parse_rec_file(hytek_dir / "BChampRecord-test-y.rec")
        assert rec.header.course == "SCY"
        assert rec.header.course_code == "Y"
        assert rec.header.record_set_name != ""

    def test_parse_records(self, hytek_dir):
        rec = parse_rec_file(hytek_dir / "BChampRecord-test-y.rec")
        assert len(rec.records) > 0
        r = rec.records[0]
        assert r.sex in ("Male", "Female", "Mixed")
        assert r.stroke in (
            "Freestyle", "Backstroke", "Breaststroke", "Butterfly",
            "IM", "Freestyle Relay", "Medley Relay",
        )
        assert r.distance > 0
        assert r.time_seconds > 0
        assert r.time_formatted != ""

    def test_mixed_sex_ages(self, hytek_dir):
        rec = parse_rec_file(hytek_dir / "BChamps-FunAgesAndMixedSex-y.rec")
        assert len(rec.records) > 0
        sexes = {r.sex for r in rec.records}
        assert len(sexes) > 1  # Should have mixed sexes

    def test_all_sample_files_parse(self, hytek_dir):
        for path in hytek_dir.glob("*.rec"):
            rec = parse_rec_file(path)
            assert rec.header is not None
            assert isinstance(rec.records, list)

    def test_file_not_found(self):
        with pytest.raises(Exception):
            parse_rec_file("/nonexistent/file.rec")
