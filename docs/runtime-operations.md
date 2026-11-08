# Ordered runtime operations

Generated from `sass.semantics.ordered.OPERATIONS`.

Arguments and results are bit vectors of the stated width or typed opaque handles.

| Operation | Arguments | Results | Reads | Writes | Suspends | Description |
|---|---|---|---|---|---|---|
| `warp_capture` | bits32, bits32, bits32 | warp_snapshot | warp | snapshot | yes | Capture participating lanes' values before destination writes. |
| `warp_capture64` | bits32, bits32, bits64 | warp_snapshot64 | warp | snapshot | yes | Capture participating lanes' 64-bit values before destination writes. |
| `warp_capture_predicate` | bits32, bits32, bits1 | warp_snapshot | warp | snapshot | yes | Capture participating lanes' predicates before destination writes. |
| `warp_capture_words1` | bits32, bits32, bits32, bits32 | warp_word_group | warp | snapshot | yes | Capture each participating lane's 1 operand words from the explicitly selected group. |
| `warp_capture_words2` | bits32, bits32, bits32, bits64 | warp_word_group | warp | snapshot | yes | Capture each participating lane's 2 operand words from the explicitly selected group. |
| `warp_capture_words4` | bits32, bits32, bits32, bits128 | warp_word_group | warp | snapshot | yes | Capture each participating lane's 4 operand words from the explicitly selected group. |
| `snapshot_word` | warp_word_group, bits32, bits32 | bits32 | snapshot | — | no | Read one indexed word of a lane's retained operand group. |
| `snapshot_release_words` | warp_word_group | — | — | snapshot | no | Release this participant's retained operand group. |
| `snapshot_lane` | warp_snapshot, bits32 | bits32 | snapshot | — | no | Read a lane from a retained warp snapshot. |
| `snapshot_lane64` | warp_snapshot64, bits32 | bits64 | snapshot | — | no | Read a 64-bit lane from a retained warp snapshot. |
| `snapshot_release` | warp_snapshot | — | — | snapshot | no | Release this participant's retained warp snapshot. |
| `snapshot_release64` | warp_snapshot64 | — | — | snapshot | no | Release this participant's retained 64-bit warp snapshot. |
| `snapshot_ballot` | warp_snapshot | bits32 | snapshot | — | no | Return the mask of captured predicates that are true. |
| `snapshot_match` | warp_snapshot, bits32 | bits32 | snapshot | — | no | Return the captured lanes whose 32-bit values equal the selected lane's. |
| `snapshot_match64` | warp_snapshot64, bits32 | bits32 | snapshot | — | no | Return the captured lanes whose 64-bit values equal the selected lane's. |
| `snapshot_redux_and` | warp_snapshot | bits32 | snapshot | — | no | Return the bitwise AND of captured 32-bit values. |
| `snapshot_redux_or` | warp_snapshot | bits32 | snapshot | — | no | Return the bitwise OR of captured 32-bit values. |
| `snapshot_redux_xor` | warp_snapshot | bits32 | snapshot | — | no | Return the bitwise XOR of captured 32-bit values. |
| `snapshot_redux_add` | warp_snapshot | bits32 | snapshot | — | no | Return the wrapping sum of captured 32-bit values. |
| `snapshot_redux_umin` | warp_snapshot | bits32 | snapshot | — | no | Return the unsigned minimum of captured 32-bit values. |
| `snapshot_redux_umax` | warp_snapshot | bits32 | snapshot | — | no | Return the unsigned maximum of captured 32-bit values. |
| `snapshot_redux_smin` | warp_snapshot | bits32 | snapshot | — | no | Return the signed minimum of captured 32-bit values. |
| `snapshot_redux_smax` | warp_snapshot | bits32 | snapshot | — | no | Return the signed maximum of captured 32-bit values. |
| `barrier_arrive` | bits32, bits32, bits1 | barrier_ticket | barrier | barrier, ticket | no | Arrive once at a barrier generation and retain its ticket. |
| `barrier_wait` | barrier_ticket | bits32 | barrier | — | yes | Wait for this ticket's generation and return its reduction. |
| `barrier_wait_ready` | barrier_ticket | — | barrier | — | yes | Wait for this ticket's generation without a reduction result. |
| `barrier_release_ticket` | barrier_ticket | — | — | ticket | no | Release this participant's ticket without cancelling its arrival or an existing wait. |
| `block_barrier_arrive_implicit` | bits32, bits1, bits32 | block_barrier_ticket | barrier, block, environment | barrier, ticket, events | no | Arrive at the barrier ID with an explicit predicate and reduction kind (0: synchronization, 1: AND, 2: OR, 3: POPC). Cooperative participants are the block's threads, excluding exited non-arrivers. Standalone execution requires explicit synchronization acknowledgement. |
| `block_barrier_arrive_counted` | bits32, bits32, bits1, bits32 | block_barrier_ticket | barrier, block, environment | barrier, ticket, events | no | Arrive at the barrier ID with an explicit participant count, predicate and reduction kind (0: synchronization, 1: AND, 2: OR, 3: POPC). Cooperative counts must be positive multiples of 32 within the block size. Standalone execution requires explicit synchronization acknowledgement. |
| `block_barrier_wait_ready` | block_barrier_ticket | — | barrier | barrier | yes | Wait for this participant's retained block barrier generation. |
| `block_barrier_defer` | block_barrier_ticket | — | — | barrier | no | Retain this participant's generation as its deferred barrier. |
| `block_barrier_release_ticket` | block_barrier_ticket | — | — | ticket | no | Release a block barrier handle without cancelling its arrival, deferred barrier, or issued wait. |
| `block_barrier_wait_deferred` | — | — | barrier | barrier | yes | Wait for this participant's pending deferred barrier, if present. An isolated environment without a coordinator has no pending generation. |
| `block_barrier_result` | — | bits32 | barrier, environment | — | no | Read this participant's published block barrier reduction result, or the standalone environment's explicitly supplied result. |
| `block_barrier_state` | bits32 | bits32 | barrier, environment | — | no | Read the explicit barrier ID's current block barrier state, or the standalone environment's explicitly supplied state. |
| `warp_sync_arrive` | bits32 | warp_sync_ticket | warp, environment | warp_barrier, ticket, events | no | Arrive once with an explicit member mask and retain a warp synchronization ticket. The mask must include this lane; participating live lanes must arrive with matching masks. Standalone execution requires explicit synchronization acknowledgement. |
| `warp_sync_arrive_exclusive` | bits32 | warp_sync_ticket | environment | ticket, events | no | Request explicit standalone acknowledgement of exclusive warp synchronization with this mask. Cooperative execution is unsupported; an untaken predicate makes no request. |
| `warp_sync_wait` | warp_sync_ticket | — | warp_barrier | — | yes | Wait for the ticket's live masked participants to arrive, retaining the ticket's generation across suspension. |
| `warp_sync_release_ticket` | warp_sync_ticket | — | — | ticket | no | Release the participant's ticket without cancelling its arrival or an existing wait. |
| `barrier_packed_arrive_ack` | bits32, bits32 | — | environment | events | no | Acknowledge two captured packed barrier operands in the standalone environment. Cooperative interpretation remains unresolved. |
| `barrier_packed_wait_ack` | bits32, bits32 | — | environment | events | no | Acknowledge waiting with two captured packed barrier operands in the standalone environment. Cooperative interpretation remains unresolved. |
| `barrier_packed_defer_ack` | bits32, bits32 | — | environment | events | no | Acknowledge deferred waiting with two captured packed barrier operands in the standalone environment. Cooperative interpretation remains unresolved. |
| `barrier_packed_reduce_ack` | bits32, bits32, bits32, bits1 | — | environment | events | no | Acknowledge the encoded reduction, two captured packed barrier operands and captured predicate contribution in the standalone environment. Cooperative interpretation remains unresolved. |
| `barrier_packed_reduce_defer_ack` | bits32, bits32, bits32, bits1 | — | environment | events | no | Acknowledge deferred reduction with the encoded reduction, two captured packed barrier operands and captured predicate contribution in the standalone environment. Cooperative interpretation remains unresolved. |
| `barrier_scan_ack` | bits32, bits32 | — | environment | events | no | Acknowledge scanning with the captured barrier ID and count in the standalone environment. Cooperative interpretation remains unresolved. |
| `barrier_all_ack` | — | — | environment | events | no | Acknowledge synchronization of all barriers in the standalone environment. Cooperative interpretation remains unresolved. |
| `barrier_all_defer_ack` | — | — | environment | events | no | Acknowledge deferred synchronization of all barriers in the standalone environment. Cooperative interpretation remains unresolved. |
| `warp_mask_ack` | bits32 | — | environment | events | no | Acknowledge synchronization with the captured constant-bank mask in the standalone environment. Cooperative execution remains unsupported. |
| `warp_collective_all_ack` | bits64 | — | environment | events | no | Acknowledge collective synchronization with the explicit target and implicitly encoded ALL mode in the standalone environment. Cooperative execution remains unsupported. |
| `warp_collective_mask_ack` | bits32, bits64 | — | environment | events | no | Acknowledge collective synchronization with the captured mask and explicit target in the standalone environment. Cooperative execution remains unsupported. |
| `warp_relative_all_ack` | bits64, bits32 | — | environment | events | no | Acknowledge collective synchronization with the encoded immediate, REL control and implicitly encoded ALL mode in the standalone environment. Cooperative execution remains unsupported. |
| `warp_relative_mask_ack` | bits32, bits64, bits32 | — | environment | events | no | Acknowledge collective synchronization with the captured mask, encoded immediate and REL control in the standalone environment. Cooperative execution remains unsupported. |
| `cooperative_context` | — | bits1 | environment | — | no | Read whether this execution has a shared cooperative coordinator. |
| `convergence_enter` | bits32, bits64, bits32 | — | warp | convergence | no | Enter the explicit convergence ID and target with the captured enabled-member mask. Each member enters once; reusing an active ID faults. |
| `convergence_arrive` | bits32 | convergence_ticket | convergence | convergence, ticket | no | Arrive once at this lane's previously entered convergence ID and retain its generation. Missing membership faults. |
| `convergence_wait` | convergence_ticket | — | convergence | — | yes | Wait for every live member of the retained convergence generation to arrive. Exited members do not block completion. |
| `convergence_release_ticket` | convergence_ticket | — | — | ticket | no | Release this convergence handle without cancelling an issued wait or changing a later generation. |
| `convergence_ack_enter` | bits32, bits64 | — | environment | events | no | Request explicit standalone BSSY acknowledgement for the convergence ID and target; no concurrent scheduler is introduced. |
| `convergence_ack_wait` | bits32 | — | environment | events | no | Request explicit standalone BSYNC acknowledgement for the convergence ID; no concurrent scheduler is introduced. |
| `convergence_enter_reliable` | bits32, bits64, bits32 | — | warp | convergence | no | Enter an explicit RELIABLE convergence generation. The current coordinator preserves the ordinary BSSY membership and ID-reuse rules for this encoded mode. |
| `convergence_enter_reconvergent` | bits32, bits64, bits32 | — | warp | convergence | no | Enter an explicit RECONVERGENT convergence generation. The current coordinator preserves the ordinary BSSY membership and ID-reuse rules for this encoded mode. |
| `convergence_arrive_reliable` | bits32 | convergence_ticket | convergence | convergence, ticket | no | Arrive at an explicit RELIABLE convergence ID. The current coordinator preserves the ordinary BSYNC membership rules for this encoded mode. |
| `convergence_arrive_reconvergent` | bits32 | convergence_ticket | convergence | convergence, ticket | no | Arrive at an explicit RECONVERGENT convergence ID. The current coordinator preserves the ordinary BSYNC membership rules for this encoded mode. |
| `convergence_ack_enter_mode` | bits32, bits32, bits64 | — | environment | events | no | Request standalone BSSY acknowledgement with the explicitly encoded reliability mode, convergence ID and target. |
| `convergence_ack_wait_mode` | bits32, bits32 | — | environment | events | no | Request standalone BSYNC acknowledgement with the explicitly encoded reliability mode and convergence ID. |
| `convergence_break` | bits32 | — | environment | convergence, events | no | Request the standalone environment to break from the explicit convergence ID. |
| `convergence_state_read` | bits32 | bits32 | environment | events | no | Read the explicit convergence-state selector from the standalone environment. |
| `convergence_state_read_clear` | bits32 | bits32 | environment | convergence, events | no | Read and clear the explicit convergence-state selector in the standalone environment. |
| `convergence_barrier_copy` | bits32, bits32 | — | environment | convergence, events | no | Copy the explicit source convergence barrier into the explicit destination barrier in the standalone environment. |
| `convergence_state_import` | bits32, bits32 | — | environment | convergence, events | no | Import the explicit convergence-state selector into the explicit destination barrier in the standalone environment. |
| `convergence_exit_pc_write` | bits64 | — | environment | convergence, events | no | Write the captured 64-bit exit PC in the standalone environment. |
| `convergence_state_write` | bits32, bits32, bits32 | — | environment | convergence, events | no | Write the captured value to the explicit convergence-state selector with the explicit predicate-quad mode in the standalone environment. |
| `convergence_barrier_write` | bits32, bits32, bits32 | — | environment | convergence, events | no | Write the explicit source barrier to the explicit convergence-state selector with the explicit predicate-quad mode in the standalone environment. |
| `dependency_wait` | bits6 | — | dependencies, async | async, shared | no | Complete work in the explicitly encoded dependency slots synchronously in the CPU model. |
| `memory_fence` | bits32, bits32 | — | — | memory_order | no | Apply the explicitly selected fence semantics and memory scope. |
| `memory_order_1` | bits32 | — | — | memory_order | no | Validate the class and its encoded memory controls. |
| `memory_order_2` | bits32, bits32 | — | — | memory_order | no | Validate the class and its encoded memory controls. |
| `memory_order_3` | bits32, bits32, bits32 | — | — | memory_order | no | Validate the class and its encoded memory controls. |
| `memory_order_4` | bits32, bits32, bits32, bits32 | — | — | memory_order | no | Validate the class and its encoded memory controls. |
| `memory_order_5` | bits32, bits32, bits32, bits32, bits32 | — | — | memory_order | no | Validate the class and its encoded memory controls. |
| `memory_order_6` | bits32, bits32, bits32, bits32, bits32, bits32 | — | — | memory_order | no | Validate the class and its encoded memory controls. |
| `memory_descriptor` | bits64 | — | descriptor | — | no | Validate the selected memory descriptor. |
| `packed_async_descriptor` | bits64, bits1 | — | descriptor | — | no | Validate a packed asynchronous memory descriptor. |
| `cache_control` | bits32, bits64 | — | — | cache | no | Issue the selected data-cache request. |
| `lane_id` | — | bits32 | warp | — | no | Read the executing lane's index in its warp. |
| `active_lanes` | — | bits32 | warp | — | no | Read the mask of lanes executing this instruction. |
| `barrier_eligible_lanes` | bits32 | bits32 | warp, barrier | — | no | Select lanes from the captured active mask whose earlier block-barrier obligations are ready. This query does not consume or execute their waits. |
| `waiting_lanes` | — | bits32 | warp | — | no | Read the mask of live lanes waiting elsewhere in this warp. |
| `address_space` | bits64 | bits32 | address_ranges | — | no | Classify a generic address using the environment's memory ranges. |
| `barrier_result` | — | bits32 | barrier | — | no | Read the completed barrier's reduction result. |
| `barrier_state` | bits32 | bits32 | barrier | — | no | Read the explicitly selected barrier's state. |
| `async_issue` | bits64, bits64, bits64, bits32, bits1 | — | async | async | no | Enqueue captured bytes for asynchronous shared-memory copying. |
| `async_commit` | bits32 | — | — | async | no | Commit the current asynchronous copy group to the specified dependency slot. |
| `async_wait_groups` | bits32, bits32 | — | async | async, shared | no | Complete the oldest copy groups in a dependency slot until at most the specified count remains. |
| `async_wait_slots` | bits6 | — | async | async, shared | no | Complete all committed copy groups in the selected dependency slots. |
