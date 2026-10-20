import threading
import unittest

from backend.cameras.connections import ConnectionGate, ControlSlot


class ConnectionGateTests(unittest.TestCase):
    def queued(self, ids, priorities=()):
        gate=ConnectionGate(1);gate.prioritize(priorities)
        gate.running=1  # an existing connection attempt holds admission
        order=[];errors=[];threads=[]
        def connect(cid):
            try:
                with gate.slot(cid):order.append(cid)
            except ConnectionError:errors.append(cid)
        for count,cid in enumerate(ids,1):
            thread=threading.Thread(target=connect,args=(cid,),daemon=True);thread.start();threads.append(thread)
            with gate.condition:
                self.assertTrue(gate.condition.wait_for(lambda:sum(map(len,gate.waiting))==count,timeout=5))
        return gate,threads,order,errors

    def finish(self,gate,threads):
        with gate.condition:
            gate.running=0;gate.condition.notify_all()
        for thread in threads:
            thread.join(timeout=5);self.assertFalse(thread.is_alive())

    def test_visible_retry_precedes_waiting_background_without_starvation(self):
        gate,threads,order,errors=self.queued([100,101,*range(1,10)],range(1,10))
        self.finish(gate,threads)
        self.assertEqual(order,[*range(1,8),100,8,9,101]);self.assertEqual(errors,[])

    def test_changed_wall_reprioritizes_existing_tickets_in_fifo_order(self):
        gate,threads,order,_=self.queued([100,101,1,2],[1])
        gate.prioritize([2]);self.finish(gate,threads)
        self.assertEqual(order,[2,100,101,1])

    def test_control_connection_is_not_stuck_behind_background_warmup(self):
        gate,threads,order,_=self.queued([100,101,None,1],[1])
        gate.prioritize([2]);self.finish(gate,threads)
        self.assertEqual(order,[None,100,101,1])

    def test_shutdown_unblocks_pending_connections(self):
        gate,threads,order,errors=self.queued([1,2,3])
        gate.close();self.finish(gate,threads)
        self.assertEqual(order,[]);self.assertEqual(set(errors),{1,2,3})

    def test_control_slot_reusable_and_exceptions_release_capacity(self):
        gate=ConnectionGate(2);slot=ControlSlot(gate)
        with self.assertRaises(ValueError):
            with slot:
                with gate.slot(1):
                    self.assertEqual(gate.running,2);raise ValueError()
        self.assertEqual(gate.running,0)
        with slot:self.assertEqual(gate.running,1)
        self.assertEqual(gate.running,0)
