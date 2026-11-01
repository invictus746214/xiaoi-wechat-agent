import unittest
from store import Store
from conversation_memory import People


class PeopleTests(unittest.TestCase):
    def setUp(self):
        self.store=Store()
        self.people=People(self.store)

    def tearDown(self):
        self.store.close()

    def record(self,scene,sender,text,n=1,title='同名'):
        self.people.record('account',scene,title,[dict(local_id=n,type=1,sender=sender,text=text,outgoing=False,timestamp=100+n)],'message/message_0.db',{'person-a':'甲','person-b':'甲'})

    def test_same_person_name_and_interest_follow_identity_across_scenes(self):
        self.record('person-a','person-a','叫我小杜',1)
        self.record('person-a','person-a','我喜欢猫',2)
        context=self.people.context('account','person-a','test@chatroom','我喜欢什么')
        self.assertIn('小杜',context)
        self.assertIn('喜欢猫',context)
        self.assertNotIn('小杜',self.people.context('account','person-b','test@chatroom','我是谁'))

    def test_private_details_stay_out_of_group_context(self):
        self.record('person-a','person-a','我的门锁密码是123456',1)
        self.record('test@chatroom','person-a','我周六去公园',2)
        group=self.people.context('account','person-a','test@chatroom','之前说过什么')
        self.assertIn('公园',group)
        self.assertNotIn('123456',group)
        private=self.people.context('account','person-a','person-a','门锁')
        self.assertIn('123456',private)
        self.assertNotIn('公园',private)

    def test_preferences_correct_without_quote_or_other_speaker_contamination(self):
        self.record('person-a','person-a','我喜欢猫',1)
        self.record('test@chatroom','person-a','我不喜欢猫',2)
        self.record('test@chatroom','person-b','我喜欢狗',3)
        self.record('person-a','person-a','他说“我喜欢狗”',4)
        context=self.people.context('account','person-a','test@chatroom','兴趣')
        self.assertIn('不喜欢猫',context)
        self.assertNotIn('喜欢狗',context)
        self.assertEqual(self.people.turn_count(),4)
        self.record('test@chatroom','person-a','我不喜欢猫',2)
        self.assertEqual(self.people.turn_count(),4)

    def test_account_isolation_and_sensitive_topics_are_not_shared(self):
        self.record('person-a','person-a','我喜欢讨论病史',1)
        self.record('person-a','person-a','叫我小杜',2)
        self.assertNotIn('病史',self.people.context('account','person-a','group@chatroom','我是谁'))
        self.assertNotIn('小杜',self.people.context('other-account','person-a','group@chatroom','我是谁'))


if __name__=='__main__': unittest.main()
