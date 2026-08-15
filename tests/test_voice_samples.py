"""Voice-sample selection (rpi/specs/api.md#get-v1sessionsidspeakers).

The filter-and-rank is pure, so it tests without a store, a transcript file, or
ffmpeg. What matters is that it does *not* behave like the longest-turn rule it
replaced: turns grow long through rambling or merged noise, so length is the
wrong signal.
"""

from earshot.jobs.transcript import MAX_SAMPLES, Segment, voice_samples


def turn(start: float, dur: float, text: str = "a perfectly ordinary sentence here") -> Segment:
    return Segment(start, start + dur, text, "Speaker 1")


def test_no_turns_yields_no_samples():
    assert voice_samples([]) == []


def test_drops_turns_shorter_than_two_seconds():
    picks = voice_samples([turn(0, 1.4, "Yeah."), turn(100, 4.0)])
    assert [p.start for p in picks] == [100]


def test_drops_stutter_artifacts_by_distinct_word_ratio():
    stutter = turn(0, 8.1, "If we can, if we can, if we can, sorry, if we can just get it done.")
    clean = turn(200, 4.0)
    picks = voice_samples([stutter, clean])
    assert [p.start for p in picks] == [200]


def test_prefers_four_seconds_over_the_longest_turn():
    # Six well-spaced candidates, only five slots — which one is dropped shows the
    # ranking. The rule this replaced would have picked the 30s turn first; here it
    # is the only one that does not survive.
    durations = [30.0, 4.0, 12.0, 3.5, 20.0, 4.5]
    picks = voice_samples([turn(i * 120, d) for i, d in enumerate(durations)])
    assert len(picks) == MAX_SAMPLES
    assert 0 not in [p.start for p in picks]  # the 30s turn, ranked last
    assert {p.end - p.start for p in picks} == {4.0, 12.0, 3.5, 20.0, 4.5}


def test_spreads_picks_across_the_session():
    # Four good turns bunched inside 60s: the spread rule keeps them apart.
    bunched = [turn(0, 4.0), turn(10, 4.1), turn(20, 3.9), turn(30, 4.2)]
    far = turn(600, 4.0)
    picks = voice_samples(bunched + [far])
    assert len(picks) == 2
    assert [p.start for p in picks] == [0, 600]


def test_relaxes_spacing_when_it_would_leave_fewer_than_two():
    # All good turns inside one 60s window — better to offer several close
    # candidates than to hand back a single one.
    picks = voice_samples([turn(0, 4.0), turn(10, 4.1), turn(20, 3.9)])
    assert len(picks) == 3


def test_falls_back_to_longest_when_everything_is_filtered():
    # Both unusable: one too short, one a stutter. A label with turns always
    # offers at least one sample.
    short = turn(0, 0.8, "Yeah.")
    stutter = turn(100, 9.0, "if we can if we can if we can if we can")
    picks = voice_samples([short, stutter])
    assert len(picks) == 1
    assert picks[0].start == 100  # the longest of the two


def test_caps_at_five_samples():
    picks = voice_samples([turn(i * 120, 4.0) for i in range(12)])
    assert len(picks) == MAX_SAMPLES


def test_returns_start_ordered():
    picks = voice_samples([turn(600, 4.0), turn(0, 3.9), turn(300, 4.2)])
    assert [p.start for p in picks] == sorted(p.start for p in picks)


def test_selection_is_deterministic():
    turns = [turn(0, 4.0), turn(200, 4.0), turn(400, 3.0), turn(600, 5.0)]
    assert [p.start for p in voice_samples(turns)] == [p.start for p in voice_samples(turns)]


def test_empty_text_is_not_a_candidate():
    picks = voice_samples([turn(0, 5.0, "   "), turn(100, 4.0)])
    assert [p.start for p in picks] == [100]
