"""Tests for Phase 1, Component 2, milestone M1: event queue and timers."""

import os
import pathlib
import subprocess
import sys

import pytest

from arq.errors import SimulationInvariantError
from arq.events import Event, EventKind, Priority, TimeoutPayload
from arq.scheduler import EventQueue, Timers

ARRIVAL = EventKind.FRAME_ARRIVAL
ACK = EventKind.ACK_ARRIVAL
TIMEOUT = EventKind.TIMEOUT
SLOT = EventKind.TRANSMISSION_SLOT


def drain(queue):
    """Pop every event; return them in processing order."""
    events = []
    while len(queue):
        events.append(queue.pop())
    return events


# --- event kinds and priorities ---------------------------------------------

def test_priorities_are_arrivals_then_timeouts_then_slots():
    assert Priority.ARRIVAL < Priority.TIMEOUT < Priority.TRANSMISSION_SLOT
    assert ARRIVAL.priority == ACK.priority == Priority.ARRIVAL
    assert TIMEOUT.priority == Priority.TIMEOUT
    assert SLOT.priority == Priority.TRANSMISSION_SLOT


def test_every_event_kind_has_a_priority():
    for kind in EventKind:
        assert isinstance(kind.priority, Priority)


def test_events_are_immutable():
    event = EventQueue().schedule(3, SLOT)
    with pytest.raises(AttributeError):
        event.time = 4


# --- ordering ---------------------------------------------------------------

def test_events_come_out_in_time_order():
    q = EventQueue()
    for t in (5, 1, 3, 0, 4):
        q.schedule(t, SLOT)
    assert [e.time for e in drain(q)] == [0, 1, 3, 4, 5]


def test_same_tick_order_is_arrival_then_timeout_then_slot():
    q = EventQueue()
    # Scheduled in the "wrong" order on purpose.
    q.schedule(7, SLOT)
    q.schedule(7, TIMEOUT, TimeoutPayload("base", 1))
    q.schedule(7, ACK)
    assert [e.kind for e in drain(q)] == [ACK, TIMEOUT, SLOT]


def test_ack_at_the_deadline_is_processed_before_the_timeout():
    q = EventQueue()
    q.schedule(5, TIMEOUT, TimeoutPayload("base", 1))  # deadline 5
    q.schedule(5, ACK)                                 # ACK arrives at 5
    first, second = drain(q)
    assert (first.kind, second.kind) == (ACK, TIMEOUT)


def test_equal_time_and_priority_use_insertion_order():
    q = EventQueue()
    a = q.schedule(2, ACK)
    b = q.schedule(2, ARRIVAL)
    c = q.schedule(2, ACK)
    assert [e.event_id for e in drain(q)] == [a.event_id, b.event_id, c.event_id]


def test_time_beats_priority_and_priority_beats_insertion():
    q = EventQueue()
    q.schedule(3, ARRIVAL)   # id 0
    q.schedule(2, SLOT)      # id 1
    q.schedule(2, ARRIVAL)   # id 2
    q.schedule(2, TIMEOUT)   # id 3
    assert [(e.time, e.kind, e.event_id) for e in drain(q)] == [
        (2, ARRIVAL, 2), (2, TIMEOUT, 3), (2, SLOT, 1), (3, ARRIVAL, 0),
    ]


def test_sort_key_is_time_priority_insertion_counter():
    event = EventQueue().schedule(4, TIMEOUT)
    assert event.sort_key == (4, 1, 0)


# --- event ids --------------------------------------------------------------

def test_event_ids_follow_scheduling_order_not_processing_order():
    q = EventQueue()
    late = q.schedule(9, SLOT)
    early = q.schedule(1, SLOT)
    assert (late.event_id, early.event_id) == (0, 1)
    assert [e.event_id for e in drain(q)] == [1, 0]


def test_event_ids_keep_increasing_while_events_are_processed():
    q = EventQueue()
    q.schedule(0, SLOT)
    q.pop()
    assert q.schedule(1, SLOT).event_id == 1
    assert q.schedule(1, SLOT).event_id == 2


def test_payload_is_carried_unchanged():
    payload = TimeoutPayload(timer_key=3, token=12)
    q = EventQueue()
    q.schedule(8, TIMEOUT, payload)
    assert q.pop().payload is payload


# --- the clock and rejecting the past ---------------------------------------

def test_now_starts_at_zero_and_follows_popped_events():
    q = EventQueue()
    assert q.now == 0
    q.schedule(4, SLOT)
    q.schedule(6, SLOT)
    q.pop()
    assert q.now == 4
    q.pop()
    assert q.now == 6


def test_scheduling_at_an_earlier_tick_is_rejected():
    q = EventQueue()
    q.schedule(5, SLOT)
    q.pop()
    with pytest.raises(SimulationInvariantError):
        q.schedule(4, ARRIVAL)


def test_negative_time_is_rejected():
    with pytest.raises(SimulationInvariantError):
        EventQueue().schedule(-1, SLOT)


def test_later_priority_at_the_current_tick_is_allowed():
    # Processing a timeout at tick 8 may start a retransmission at tick 8.
    q = EventQueue()
    q.schedule(8, TIMEOUT)
    q.pop()
    slot = q.schedule(8, SLOT)
    assert q.pop() is slot


def test_same_priority_at_the_current_tick_is_allowed():
    q = EventQueue()
    q.schedule(3, TIMEOUT)
    q.pop()
    assert q.schedule(3, TIMEOUT).time == 3


def test_earlier_priority_at_the_current_tick_is_rejected():
    # An arrival scheduled while a timeout at the same tick is being
    # processed would break "arrivals before timeouts".
    q = EventQueue()
    q.schedule(8, TIMEOUT)
    q.pop()
    with pytest.raises(SimulationInvariantError):
        q.schedule(8, ACK)


@pytest.mark.parametrize("bad_time", [2.0, "3", None, True])
def test_non_integer_times_are_rejected(bad_time):
    with pytest.raises(SimulationInvariantError):
        EventQueue().schedule(bad_time, SLOT)


def test_unknown_event_kind_is_rejected():
    with pytest.raises(SimulationInvariantError):
        EventQueue().schedule(1, "frame_arrival")


def test_popping_an_empty_queue_is_an_invariant_error():
    with pytest.raises(SimulationInvariantError):
        EventQueue().pop()


def test_rejected_schedule_leaves_the_queue_unchanged():
    q = EventQueue()
    q.schedule(5, SLOT)
    q.pop()
    with pytest.raises(SimulationInvariantError):
        q.schedule(1, SLOT)
    assert len(q) == 0
    assert q.schedule(5, SLOT).event_id == 1  # counter not consumed


# --- timers -----------------------------------------------------------------

def test_started_timer_is_running_with_its_token_and_deadline():
    timers = Timers()
    token = timers.start("base", deadline=8)
    assert timers.is_running("base")
    assert timers.token("base") == token
    assert timers.deadline("base") == 8
    assert timers.is_valid("base", token)


def test_restart_gives_a_new_token_and_makes_the_old_one_stale():
    timers = Timers()
    old = timers.start("base", deadline=8)
    new = timers.start("base", deadline=13)
    assert new > old
    assert timers.deadline("base") == 13
    assert timers.is_valid("base", new)
    assert not timers.is_valid("base", old)


def test_stop_makes_the_token_stale():
    timers = Timers()
    token = timers.start(2, deadline=10)
    assert timers.stop(2) is True
    assert not timers.is_running(2)
    assert timers.token(2) is None
    assert timers.deadline(2) is None
    assert not timers.is_valid(2, token)


def test_stopping_a_timer_that_is_not_running_does_nothing():
    timers = Timers()
    assert timers.stop("base") is False
    assert not timers.is_running("base")


def test_a_token_is_never_valid_for_a_timer_that_is_not_running():
    timers = Timers()
    assert not timers.is_valid("base", None)
    assert not timers.is_valid("base", 1)


def test_tokens_increase_globally_across_all_timers():
    timers = Timers()
    tokens = [
        timers.start(0, deadline=8),
        timers.start(1, deadline=9),
        timers.start(0, deadline=16),   # restart
        timers.start("base", deadline=20),
    ]
    assert tokens == [1, 2, 3, 4]


def test_tokens_are_never_reused_after_stop_and_restart():
    # Wrap-around: the same key (wire number 1) is reused for a new frame.
    timers = Timers()
    first = timers.start(1, deadline=8)
    timers.stop(1)
    second = timers.start(1, deadline=30)
    assert second != first
    assert not timers.is_valid(1, first)
    assert timers.is_valid(1, second)


def test_timers_with_different_keys_are_independent():
    timers = Timers()
    a = timers.start(0, deadline=8)
    b = timers.start(1, deadline=9)
    timers.stop(0)
    assert not timers.is_valid(0, a)
    assert timers.is_valid(1, b)


@pytest.mark.parametrize("bad_deadline", [8.0, "8", None, True])
def test_timer_deadline_must_be_an_integer(bad_deadline):
    with pytest.raises(SimulationInvariantError):
        Timers().start("base", deadline=bad_deadline)


def test_stale_timeout_event_is_detected_after_restart():
    # Queue and timers together, as the engine will use them.
    q, timers = EventQueue(), Timers()
    old = timers.start("base", deadline=8)
    q.schedule(8, TIMEOUT, TimeoutPayload("base", old))
    new = timers.start("base", deadline=13)       # e.g. an ACK advanced the base
    q.schedule(13, TIMEOUT, TimeoutPayload("base", new))
    validity = [timers.is_valid(e.payload.timer_key, e.payload.token) for e in drain(q)]
    assert validity == [False, True]


# --- determinism ------------------------------------------------------------

def _scripted_run():
    """A fixed sequence of queue and timer calls; return what came out."""
    q, timers = EventQueue(), Timers()
    for t, kind in [(4, SLOT), (2, ACK), (4, TIMEOUT), (2, ARRIVAL), (4, ACK), (0, SLOT)]:
        q.schedule(t, kind)
    tokens = [timers.start(key, deadline=10) for key in ("base", 3, "base", 0)]
    order = [(e.time, e.kind.value, e.event_id) for e in drain(q)]
    return order, tokens


def test_same_calls_give_the_same_result_in_the_same_process():
    assert _scripted_run() == _scripted_run()


def test_same_calls_give_the_same_result_across_processes():
    # Different hash randomisation must not change the order or tokens.
    code = (
        "from tests.test_scheduler import _scripted_run;"
        "print(_scripted_run())"
    )
    backend = pathlib.Path(__file__).resolve().parents[1]
    outputs = []
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(backend)}
        result = subprocess.run([sys.executable, "-c", code], env=env, cwd=backend,
                                capture_output=True, text=True, check=True)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert outputs[0].startswith("([(0, 'transmission_slot', 5), (2, 'ack_arrival', 1)")
