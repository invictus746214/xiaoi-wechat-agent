import sqlite3
from pathlib import Path
import tempfile
import unittest
import wechat_db


class NativeThreadTests(unittest.TestCase):
    def test_comments_keep_ids_emoji_reference_and_deletion_separate_from_likes(self):
        xml='''<SnsDataItem><TimelineObject><id>900</id><username>self</username><createTime>100</createTime><contentDesc>own post</contentDesc></TimelineObject>
        <LocalExtraInfo><like_user_list><user_comment><comment_id>5</comment_id><username>liker</username></user_comment></like_user_list>
        <comment_user_list>
        <user_comment><comment_id>3</comment_id><username>a</username><nickname>甲</nickname><content>你好</content><create_time>101</create_time><ref_comment_id>0</ref_comment_id></user_comment>
        <user_comment><comment_id>35</comment_id><username>b</username><nickname>乙</nickname><create_time>102</create_time><comment_emojiinfo_count>1</comment_emojiinfo_count><emojilist><emojiinfo><desc>打招呼</desc></emojiinfo></emojilist></user_comment>
        <user_comment><comment_id>36</comment_id><username>self</username><content>回复</content><create_time>103</create_time><ref_comment_id>3</ref_comment_id></user_comment>
        <user_comment><comment_id>37</comment_id><username>a</username><content>已删除</content><b_deleted>1</b_deleted></user_comment>
        </comment_user_list></LocalExtraInfo></SnsDataItem>'''
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'sns.db'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE SnsTimeLine(tid INTEGER,content TEXT)')
                db.execute('INSERT INTO SnsTimeLine VALUES(1,?)',(xml,))
                db.execute('CREATE TABLE SnsMessage_tmp3(feed_id INTEGER,type INTEGER,from_username TEXT,from_nickname TEXT,content TEXT)')
            db.close()
            reader=wechat_db.WeChatDB.__new__(wechat_db.WeChatDB);reader._connect=lambda _:sqlite3.connect(path)
            post=reader.moments()[0]
            self.assertIn('liker',post['likes'])
            thread=post.get('thread',[])
            self.assertEqual([c['id'] for c in thread],['3','35','36','37'])
            self.assertEqual(thread[1]['kind'],'emoji')
            self.assertIn('打招呼',thread[1]['text'])
            self.assertEqual(thread[2]['ref_id'],'3')
            self.assertTrue(thread[3]['deleted'])


if __name__=='__main__':unittest.main()
