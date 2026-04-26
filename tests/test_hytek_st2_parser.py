import pytest

from hytek_st2_parser import parse_st2_file


class TestParseSt2File:
    def test_parse_header(self, hytek_dir):
        st2 = parse_st2_file(hytek_dir / "TimeStd.st2")
        assert len(st2.header.standards) > 0
        for std in st2.header.standards:
            assert std.tag != ""

    def test_parse_events(self, hytek_dir):
        st2 = parse_st2_file(hytek_dir / "TimeStd.st2")
        assert len(st2.events) > 0
        ev = st2.events[0]
        assert ev.sex in ("Male", "Female", "Mixed")
        assert ev.distance > 0
        assert ev.stroke != ""

    def test_qualifying_times_present(self, hytek_dir):
        st2 = parse_st2_file(hytek_dir / "TimeStd.st2")
        has_times = False
        for ev in st2.events:
            for course in ev.courses:
                for qt in course.times:
                    has_times = True
                    assert qt.time_seconds > 0
                    assert qt.time_formatted != ""
        assert has_times

    def test_mixed_standards(self, hytek_dir):
        st2 = parse_st2_file(hytek_dir / "MixedTimeStandards.st2")
        assert len(st2.events) > 0

    def test_three_courses(self, hytek_dir):
        st2 = parse_st2_file(hytek_dir / "MixedTimeStandards-ThreeCourses.st2")
        all_courses = set()
        for ev in st2.events:
            for cs in ev.courses:
                all_courses.add(cs.course)
        assert "SCY" in all_courses
        assert "SCM" in all_courses
        assert "LCM" in all_courses

    def test_all_sample_files_parse(self, hytek_dir):
        for path in hytek_dir.glob("*.st2"):
            st2 = parse_st2_file(path)
            assert st2.header is not None
            assert isinstance(st2.events, list)

    def test_event_age_groups(self, hytek_dir):
        st2 = parse_st2_file(hytek_dir / "TimeStd.st2")
        for ev in st2.events:
            if ev.age_group_min is not None:
                assert ev.age_group_min > 0
            if ev.age_group_max is not None:
                assert ev.age_group_max > 0

    def test_file_not_found(self):
        with pytest.raises(Exception):
            parse_st2_file("/nonexistent/file.st2")
