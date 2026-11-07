import pytest
from sass.emulate import machine
from sass.semantics.runtime import UnsupportedSemantics


pytestmark = pytest.mark.native


def test_eligible_lanes_reads_blocked_members_without_consuming_waits(participants):
    lanes = participants(4)
    ticket = lanes[0].arrive(0, 1, 0)
    lanes[0].bind(ticket, deferred=True)
    assert lanes[1].eligible_lanes(15) == 14
    assert lanes[0].pending() is not None
    lanes[0].bind(ticket)
    assert lanes[1].eligible_lanes(15) == 14
    for lane in lanes[1:]: lane.arrive(0, 1, 0)
    assert lanes[1].eligible_lanes(15) == 15
    lanes[0].exit()
    assert lanes[1].eligible_lanes(15) == 14
    with pytest.raises(UnsupportedSemantics): lanes[1].eligible_lanes(31)


@pytest.fixture(scope="module")
def library():
    return machine.Library('#include "sass/abi.hpp"\n', "coordination").lib


@pytest.fixture
def participants(library):
    states = []

    def create(count):
        state = library.sass_coordination_new(count)
        states.append(state)
        return [machine.Coordination(library, state, index) for index in range(count)]

    yield create
    for state in states:
        library.sass_coordination_free(state)


def test_convergence_generations_survive_reuse_and_ownership_checks(participants):
    lanes = participants(2)
    for lane in lanes: lane.enter_convergence(3, 0x1200, 3)
    first = lanes[0].arrive_convergence(3)
    assert not first.ready()
    with pytest.raises(UnsupportedSemantics, match="repeats"): lanes[0].arrive_convergence(3)
    other = lanes[1].arrive_convergence(3)
    assert first.ready() and other.ready()
    for lane in lanes: lane.enter_convergence(3, 0x1300, 3)
    next_ = lanes[0].arrive_convergence(3)
    assert not next_.ready() and first.ready()
    lanes[0].check_convergence(first)
    with pytest.raises(UnsupportedSemantics, match="another coordinator or participant"):
        lanes[1].check_convergence(first)
    with pytest.raises(UnsupportedSemantics, match="another coordinator or participant"):
        participants(1)[0].check_convergence(first)
    lanes[1].arrive_convergence(3)
    assert next_.ready()


def test_convergence_exits_release_entered_and_unentered_members(participants):
    lanes = participants(3)
    for lane in lanes[:2]: lane.enter_convergence(0, 0x1200, 7)
    ticket = lanes[0].arrive_convergence(0)
    lanes[2].exit()
    assert not ticket.ready()
    lanes[1].exit()
    assert ticket.ready()


def test_convergence_ids_and_warps_are_independent(participants):
    lanes = participants(34)
    for id in (0, 1):
        for index in (0, 1, 32, 33): lanes[index].enter_convergence(id, 0x1200, 3)
    tickets = {(id, index): lanes[index].arrive_convergence(id) for id in (0, 1) for index in (0, 32)}
    lanes[1].arrive_convergence(0)
    assert tickets[0, 0].ready()
    assert not any(ticket.ready() for key, ticket in tickets.items() if key != (0, 0))
    for id, index in ((1, 1), (0, 33), (1, 33)): lanes[index].arrive_convergence(id)
    assert all(ticket.ready() for ticket in tickets.values())


def test_convergence_rejects_reentry_and_invalid_membership(participants):
    lanes = participants(2)
    with pytest.raises(UnsupportedSemantics, match="no matching BSSY"): lanes[0].arrive_convergence(0)
    for id, members in ((16, 1), (0, 2), (0, 7)):
        with pytest.raises(UnsupportedSemantics, match="convergence"):
            lanes[0].enter_convergence(id, 0x1200, members)
    lanes[0].enter_convergence(0, 0x1200, 3)
    for lane, target, members in ((0, 0x1200, 3), (1, 0x1300, 3), (1, 0x1200, 2)):
        with pytest.raises(UnsupportedSemantics, match="active convergence ID"):
            lanes[lane].enter_convergence(0, target, members)
    lanes[1].enter_convergence(0, 0x1200, 3)
    lanes[1].exit()
    with pytest.raises(UnsupportedSemantics, match="exited"): lanes[1].arrive_convergence(0)


@pytest.mark.parametrize("reduction,expected", [(0, 16), (1, 0), (2, 0xffffffff), (3, 16)])
@pytest.mark.parametrize("counted", [False, True])
def test_block_barrier_reductions_and_generation_reuse(participants, reduction, expected, counted):
    lanes = participants(32)
    tickets = []
    for index, lane in enumerate(lanes):
        ticket = lane.arrive(3, index % 2, reduction, 32 if counted else None)
        lane.bind(ticket)
        tickets.append(ticket)
        assert ticket.ready() == (index == 31)
    assert all(ticket.ready() for ticket in tickets)
    assert [lane.read() for lane in lanes] == [expected] * 32
    assert lanes[0].read(3) == 0
    next_ticket = lanes[0].arrive(3, 1, reduction, 32 if counted else None)
    assert not next_ticket.ready()
    assert all(ticket.ready() for ticket in tickets)


def test_implicit_barrier_excludes_only_exited_non_arrivers(participants):
    lanes = participants(4)
    first = lanes[0].arrive(0, 1, 3)
    lanes[0].bind(first)
    lanes[0].exit()
    lanes[1].exit()
    second = lanes[2].arrive(0, 1, 3)
    lanes[2].bind(second)
    assert not first.ready()
    third = lanes[3].arrive(0, 0, 3)
    assert all(ticket.ready() for ticket in [first, second, third])
    assert [lane.read() for lane in lanes] == [2, 0, 2, 2]


def test_exit_completes_a_pending_implicit_barrier(participants):
    lanes = participants(2)
    ticket = lanes[0].arrive(0, 1, 1)
    lanes[0].bind(ticket, deferred=True)
    retained = lanes[0].pending()
    del ticket
    assert not retained.ready()
    lanes[1].exit()
    assert retained.ready()
    assert lanes[0].pending() is None
    assert lanes[0].read() == 0xffffffff


def test_warp_masks_complete_independently_and_retain_old_tickets(participants):
    lanes = participants(4)
    first = lanes[0].arrive_warp(3)
    second = lanes[2].arrive_warp(12)
    assert not first.ready() and not second.ready()
    assert lanes[1].arrive_warp(3).ready() and first.ready()
    assert not second.ready()
    assert lanes[3].arrive_warp(12).ready() and second.ready()
    later = lanes[0].arrive_warp(3)
    assert first.ready() and not later.ready()
    lanes[1].arrive_warp(3)
    assert later.ready()


def test_warp_masks_exclude_absent_and_exited_lanes(participants):
    lanes = participants(34)
    last = lanes[32].arrive_warp(0xffffffff)
    assert not last.ready()
    lanes[33].exit()
    assert last.ready()
    first = lanes[0].arrive_warp(3)
    assert not first.ready()
    lanes[1].exit()
    assert first.ready()


def test_matching_masks_in_different_warps_have_independent_generations(participants):
    lanes = participants(34)
    first, last = lanes[0].arrive_warp(3), lanes[32].arrive_warp(3)
    lanes[1].arrive_warp(3)
    assert first.ready() and not last.ready()
    lanes[33].arrive_warp(3)
    assert last.ready()


def test_warp_masks_fault_when_the_lane_is_omitted_or_members_disagree(participants):
    lanes = participants(2)
    with pytest.raises(UnsupportedSemantics, match="omits the lane"): lanes[0].arrive_warp(2)
    pending = lanes[0].arrive_warp(3)
    with pytest.raises(UnsupportedSemantics, match="disagree"): lanes[1].arrive_warp(2)
    assert not pending.ready()


def test_warp_ticket_ownership_and_repeated_arrivals(participants):
    lanes = participants(2)
    ticket = lanes[0].arrive_warp(3)
    lanes[0].check_warp(ticket)
    with pytest.raises(UnsupportedSemantics, match="repeats"): lanes[0].arrive_warp(3)
    with pytest.raises(UnsupportedSemantics, match="another coordinator or participant"):
        lanes[1].check_warp(ticket)
    with pytest.raises(UnsupportedSemantics, match="another coordinator or participant"):
        participants(2)[0].check_warp(ticket)


@pytest.mark.parametrize("count", [0, 1, 31, 33, 96])
def test_counted_barrier_rejects_invalid_counts(participants, count):
    with pytest.raises(UnsupportedSemantics, match="participant count"):
        participants(64)[0].arrive(0, 1, 0, count)


@pytest.mark.parametrize("change", ["count", "implicit", "reduction", "duplicate"])
def test_block_barrier_rejects_incompatible_arrivals(participants, change):
    lanes = participants(64)
    lanes[0].arrive(0, 1, 0, 32)
    lane, count, reduction = {
        "count": (1, 64, 0), "implicit": (1, None, 0),
        "reduction": (1, 32, 1), "duplicate": (0, 32, 0),
    }[change]
    with pytest.raises(UnsupportedSemantics, match="incompatible|repeats"):
        lanes[lane].arrive(0, 1, reduction, count)


def test_block_tickets_validate_coordinator_and_participant(participants):
    first, second = participants(2), participants(2)
    ticket = first[0].arrive(0, 1, 0)
    for lane in [first[1], second[0]]:
        with pytest.raises(UnsupportedSemantics, match="another coordinator or participant"):
            lane.bind(ticket)


def test_retained_ready_ticket_does_not_discard_a_new_deferred_generation(participants):
    lanes = participants(2)
    old = lanes[0].arrive(0, 1, 0)
    lanes[1].arrive(0, 1, 0)
    current = lanes[0].arrive(0, 1, 0)
    lanes[0].bind(current, deferred=True)
    for deferred in [False, True]:
        lanes[0].bind(old, deferred=deferred)
        assert not lanes[0].pending().ready()
    lanes[1].arrive(0, 1, 0)
    assert current.ready() and old.ready()
    assert lanes[0].pending() is None


@pytest.mark.parametrize("reduction,tag,sum", [(0, 8, 16), (1, 10, 16), (2, 11, 16), (3, 9, 16)])
def test_barrier_state_and_arrival_result_publication(participants, reduction, tag, sum):
    lanes = participants(64)
    tickets = [lane.arrive(7, index % 2, reduction) for index, lane in enumerate(lanes[:32])]
    lanes[0].bind(tickets[0])
    lanes[1].bind(tickets[1], deferred=True)
    assert lanes[0].read(7) == tag << 28 | sum << 8 | 1
    for lane in lanes[32:]:
        lane.arrive(7, 1, reduction)
    expected = {0: 48, 1: 0, 2: 0xffffffff, 3: 48}[reduction]
    assert [lane.read() for lane in lanes] == [expected, expected] + [0] * 30 + [expected] * 32
    assert all(ticket.ready() for ticket in tickets)
    assert lanes[1].pending() is None


def test_invalid_participant_errors_cross_the_ffi(participants):
    lane = participants(1)[0]
    lane.participant = 1
    for action in [lane.pending, lane.exit, lane.read, lambda: lane.arrive(0, 1, 0)]:
        with pytest.raises(UnsupportedSemantics, match="invalid barrier participant"):
            action()
