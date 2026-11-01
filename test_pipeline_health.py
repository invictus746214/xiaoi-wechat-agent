import json,unittest
from store import Store
from pipeline_health import PipelineHealth
from reply_plan import freeze_reply,restore_reply


class PipelineHealthTests(unittest.TestCase):
    def setUp(self):
        self.store=Store();self.health=PipelineHealth(self.store)
        self.job=dict(event_id='event-1',account='account-private',username='private-person',kind='private',message='正文不应写入诊断',started=1)
    def tearDown(self):self.store.close()

    def test_phases_and_skip_reasons_persist_without_chat_content(self):
        self.health.note(self.job,'queued','claimed',now=100)
        self.health.note(self.job,'model','generating',duration_ms=123,now=101)
        self.health.note(self.job,'skipped','model_silent',now=102)
        text=str(self.store._rows('SELECT * FROM pipeline_trace'))
        for secret in ('正文不应写入诊断','account-private','private-person'):self.assertNotIn(secret,text)
        report=self.health.report(self.job,now=103)
        self.assertIn('主动沉默',report);self.assertIn('1',report)
        self.assertNotIn('正文',report)

    def test_report_is_scene_scoped_and_note_failure_cannot_break_replies(self):
        self.health.note(self.job,'uncertain','send_unknown',now=100)
        other=dict(self.job,event_id='event-2',username='other')
        self.health.note(other,'confirmed','sent',now=100)
        self.assertIn('送达未确认：1',self.health.report(self.job,now=101))
        self.assertIn('确认发送：0',self.health.report(self.job,now=101))
        self.store.close()
        self.assertFalse(self.health.note(self.job,'model','generating'))
        self.store=Store()

    def test_reply_plan_restores_semantic_choices_after_restart(self):
        job=dict(structured_reply=True,sticker_choice='a'*64,sticker_only=False)
        packed=freeze_reply(job,'这也太好笑了')
        restored={};self.assertEqual(restore_reply(restored,packed),'这也太好笑了')
        self.assertEqual(restored,job)
        nonejob=dict(structured_reply=True,sticker_choice=None,sticker_only=False)
        restored={};self.assertEqual(restore_reply(restored,freeze_reply(nonejob,'嗯')), '嗯')
        self.assertIsNone(restored['sticker_choice']);self.assertTrue(restored['structured_reply'])

    def test_reply_plan_rejects_metadata_injection_and_preserves_old_cache(self):
        self.assertEqual(restore_reply({},'旧缓存回复'),'旧缓存回复')
        with self.assertRaises(ValueError):freeze_reply(dict(structured_reply=True,sticker_choice='../../secret',sticker_only=True),'hidden')
        packed=freeze_reply(dict(structured_reply=True,sticker_choice='b'*64,sticker_only=True),'hidden')
        with self.assertRaises(ValueError):restore_reply({},packed.replace('b'*64,'bad'))

if __name__=='__main__':unittest.main()
