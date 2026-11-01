import time
import unittest
from unittest.mock import Mock,patch
from store import Store
import settings
import bridge
try:import moments_replies as m
except ImportError:m=None


class NativeReplyTests(unittest.TestCase):
    def setUp(self):
        self.store=Store()
        self.now=time.time()
        self.comment=dict(id='3',username='a',nickname='甲',text='你好',timestamp=self.now,deleted=False,ref_id='0',kind='text')
        self.post=dict(id='post1',username='self',text='own post',timestamp=self.now,thread=[self.comment])

    def tearDown(self):self.store.close()

    def test_native_targets_respect_account_post_identity_and_existing_references(self):
        self.assertIsNotNone(m,'native comment reply controller missing')
        other=dict(self.post,id='post2')
        rows=m.pending([self.post,other],'self',self.store,0)
        self.assertEqual(len(rows),2)
        self.assertNotEqual(rows[0]['event'],rows[1]['event'])
        reply=dict(self.comment,id='reply',username='self',ref_id='3')
        post=dict(self.post,thread=[self.comment,reply,dict(self.comment,id='deleted',deleted=True)])
        self.assertEqual(m.pending([post],'self',self.store,0),[])
        self.assertEqual(m.pending([dict(self.post,username='other')],'self',self.store,0),[])
        self.assertEqual(m.pending([self.post],'self',self.store,self.now+1),[])

    def test_unknown_delivery_is_not_submitted_again(self):
        self.assertIsNotNone(m,'native comment reply controller missing')
        target=m.pending([self.post],'self',self.store,0)[0]
        self.store.start_delivery(target['event'],'moment','event','message','reply')
        self.store.set_delivery_state(target['event'],'unconfirmed')
        self.assertEqual(m.pending([self.post],'self',self.store,0),[])

    def test_cutoff_survives_restart_and_does_not_replay_old_history(self):
        self.assertIsNotNone(m,'native comment reply controller missing')
        first=m.start_time(self.store,'self',self.now)
        self.assertLessEqual(first,self.now)
        self.assertLess(self.now-first,86400)
        self.assertEqual(m.start_time(self.store,'self',self.now+86400),first)

    def test_receipt_requires_new_self_comment_on_exact_post(self):
        self.assertIsNotNone(m,'native comment reply controller missing')
        old=dict(self.comment,id='old',username='self',text='reply',ref_id='0')
        new=dict(old,id='new')
        self.assertFalse(m.delivered([dict(self.post,thread=[old])],'self',self.post,self.comment,'reply',{'old'}))
        self.assertTrue(m.delivered([dict(self.post,thread=[old,new])],'self',self.post,self.comment,'reply',{'old'}))
        self.assertFalse(m.delivered([dict(self.post,id='wrong',thread=[new])],'self',self.post,self.comment,'reply',set()))

    def environment(self):
        reader=Mock();reader.account='self';reader.moments.return_value=[self.post];reader.contacts.return_value={'a':'甲'}
        return reader,settings.normalize({}),Mock(),Mock()

    def test_submitted_without_native_receipt_is_unknown_and_never_repeated(self):
        self.assertIsNotNone(m)
        reader,cfg,ui,metrics=self.environment()
        def submit(*args):
            self.assertTrue(args[-2]());return 'submitted'
        with patch('moments_replies.moments_feed.MomentsFeed'),patch('moments_replies.open_details',return_value=True),patch('moments_replies.reply_to_comment',side_effect=submit) as action,patch('moments_replies.time.sleep'):
            report=m.run(ui,cfg,self.store,metrics,lambda *_:'hello',bridge,reader=reader)
            self.assertEqual(report['replied'],[])
            self.assertEqual(self.store._rows('SELECT state FROM outbox')[0]['state'],'unconfirmed')
            m.run(ui,cfg,self.store,metrics,lambda *_:'new answer',bridge,reader=reader)
            action.assert_called_once()

    def test_unsubmitted_draft_keeps_original_text_for_retry(self):
        self.assertIsNotNone(m)
        reader,cfg,ui,metrics=self.environment()
        replier=Mock(return_value='original')
        with patch('moments_replies.moments_feed.MomentsFeed'),patch('moments_replies.open_details',return_value=True),patch('moments_replies.reply_to_comment',return_value='not_typed') as action:
            m.run(ui,cfg,self.store,metrics,replier,bridge,reader=reader)
            replier.return_value='changed'
            m.run(ui,cfg,self.store,metrics,replier,bridge,reader=reader)
            replier.assert_called_once()
            self.assertEqual(action.call_args.args[5],'original')
            self.assertEqual(self.store._rows('SELECT state FROM outbox')[0]['state'],'queued')

    def test_new_native_self_reply_confirms_delivery(self):
        self.assertIsNotNone(m)
        reader,cfg,ui,metrics=self.environment()
        replied=dict(self.post,thread=[self.comment,dict(self.comment,id='4',username='self',ref_id='3',text='hello')])
        reader.moments.side_effect=[[self.post],[self.post],[replied]]
        def submit(*args):args[-2]();return 'submitted'
        with patch('moments_replies.moments_feed.MomentsFeed'),patch('moments_replies.open_details',return_value=True),patch('moments_replies.reply_to_comment',side_effect=submit),patch('moments_replies.time.sleep'):
            result=m.run(ui,cfg,self.store,metrics,lambda *_:'hello',bridge,reader=reader)
        self.assertEqual(len(result['replied']),1)
        self.assertEqual(self.store._rows('SELECT state FROM outbox')[0]['state'],'confirmed')

    def test_emoji_reply_points_stay_in_identified_comment_row(self):
        self.assertIsNotNone(m)
        self.assertTrue(hasattr(m,'comment_points'),'emoji comment hitboxes missing')
        points=m.comment_points((421,445,438,202),(146,138,68,34),[])
        self.assertEqual(points[0],(647,600))
        self.assertTrue(all(421<=x<859 and 445<=y<647 for x,y in points))
        self.assertEqual(m.comment_points((421,445,438,202),(146,83,47,10),[(225,97,30,36)]),[(661,560)])

    def test_empty_queue_does_not_touch_moments_window(self):
        reader,cfg,ui,metrics=self.environment();reader.moments.return_value=[]
        with patch('moments_replies.moments_feed.MomentsFeed') as feed:
            m.run(ui,cfg,self.store,metrics,Mock(),bridge,reader=reader)
        feed.return_value.close.assert_not_called()

    def test_existing_publication_draft_is_preserved_if_opening_is_blocked(self):
        reader,cfg,ui,metrics=self.environment()
        with patch('moments_replies.moments_feed.MomentsFeed') as feed,patch('moments_replies.open_details',return_value=False):
            feed.return_value.poster.compose_open.return_value=True
            m.run(ui,cfg,self.store,metrics,lambda *_:'hello',bridge,reader=reader)
        feed.return_value.close.assert_not_called()

    def test_repeated_author_uses_unique_comment_body_and_ignores_quoted_names(self):
        self.assertTrue(hasattr(m,'locate_comment'),'repeated author resolver missing')
        words=[('invictus',151,20,48,12),('invictus',225,40,48,12),('invictus',151,90,48,12),('新回复',225,105,45,14)]
        found=m.locate_comment((421,445,438,202),words,'测试新回复',{'invictus'})
        self.assertEqual(found[0],(151,90,48,12))

    def test_identical_emoji_comments_from_same_author_are_not_guessed(self):
        self.assertTrue(hasattr(m,'locate_comment'),'repeated author resolver missing')
        words=[('invictus',151,20,48,12),('invictus',151,90,48,12)]
        self.assertIsNone(m.locate_comment((421,445,438,202),words,'[表情评论]',{'invictus'}))


if __name__=='__main__':unittest.main()
