"""按 ID 撤回不混淆同文插话，和消费竞争时只有一方能拿到正文。"""
import threading
import unittest

from xiaoyu.steering import SteerQueue
from .test_agent_paths import AgentTestCase, chunk


class SteerQueueTests(unittest.TestCase):
    def test_distinct_ids_preserve_order_and_replace_only_one(self):
        queue = SteerQueue()
        first, second = queue.put("same"), queue.put("same")
        self.assertNotEqual(first, second)
        self.assertTrue(queue.replace(second, "updated"))
        self.assertEqual(queue.retract(first), "same")
        self.assertEqual(queue.pending(), ((second, "updated"),))
        self.assertEqual(queue.take_all(), ["updated"])
        self.assertIsNone(queue.retract(second))
        self.assertFalse(queue.replace(second, "too late"))

    def test_consumption_and_retraction_have_one_winner(self):
        queue = SteerQueue()
        ident = queue.put("once")
        barrier = threading.Barrier(3)
        outputs = []
        def consume():
            barrier.wait(3)
            outputs.extend(queue.take_all())
        def retract():
            barrier.wait(3)
            if (text := queue.retract(ident)) is not None:
                outputs.append(text)
        workers = [threading.Thread(target=consume), threading.Thread(target=retract)]
        for worker in workers:
            worker.start()
        barrier.wait(3)
        for worker in workers:
            worker.join(3)
            self.assertFalse(worker.is_alive())
        self.assertEqual(outputs, ["once"])


class SteerIntegration(AgentTestCase):
    def test_retracted_input_never_enters_history(self):
        agent = self.build([[chunk(content="first")], [chunk(content="answer")]])
        old = agent._stream_with_recovery
        once = [False]
        def stream(*args, **kwargs):
            if not once[0]:
                once[0] = True
                removed = agent.queue_steer("撤回这句")
                kept = agent.queue_steer("旧内容")
                self.assertEqual(agent.retract_steer(removed), "撤回这句")
                self.assertTrue(agent.replace_steer(kept, "真正的补充"))
            return old(*args, **kwargs)
        agent._stream_with_recovery = stream
        agent.send("开始")
        text = str(agent.messages)
        self.assertIn("真正的补充", text)
        self.assertNotIn("撤回这句", text)
        self.assertNotIn("旧内容", text)
        self.assertEqual(agent.pending_steers(), ())
