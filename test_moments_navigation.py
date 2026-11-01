import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch
import bridge
import moments
import moments_feed
from test_moments_feed import Node,Rect,FakePointer,FakeUI


class NavigationTests(unittest.TestCase):
    def test_existing_background_moments_window_is_activated(self):
        frame=Node('Moments','frame',Rect(420,120,440,560))
        app=Node('','application',Rect(0,0,1280,800),[frame])
        pointer=FakePointer();pointer.focus_window=Mock(return_value=True)
        poster=moments.MomentPoster(FakeUI(app),pointer=pointer,pause=lambda _:None)
        self.assertTrue(poster.open_feed())
        pointer.focus_window.assert_called_once_with('Moments')
        pointer.focus_window.reset_mock()
        feed=moments_feed.MomentsFeed(FakeUI(app),pointer=pointer,pause=lambda _:None)
        self.assertTrue(feed.open())
        pointer.focus_window.assert_called_once_with('Moments')

    def test_close_uses_named_close_and_never_sends_escape(self):
        close=Node('Close','push button',Rect(807,131,40,28))
        frame=Node('Moments','frame',Rect(420,120,440,560),[close])
        app=Node('','application',Rect(0,0,1280,800),[frame])
        pointer=FakePointer(on_tap=lambda *_:setattr(app,'childCount',0));pointer.focus_window=Mock(return_value=True)
        feed=moments_feed.MomentsFeed(FakeUI(app),pointer=pointer,pause=lambda _:None)
        self.assertTrue(feed.close())
        self.assertEqual(pointer.keys,[])
        self.assertEqual(pointer.taps,[(827,145)])

    def test_notification_entry_skips_hidden_zero_sized_buttons(self):
        hidden=Node('Messages','push button',Rect(0,0,0,0))
        messages=Node('Messages','push button',Rect(433,127,40,36))
        frame=Node('Moments','frame',Rect(420,120,440,560),[messages,hidden])
        pointer=FakePointer()
        ui=SimpleNamespace(pointer=lambda:pointer,app=Node('','application',Rect(0,0,1280,800),[frame]))
        poster=Mock();poster.feed.return_value=frame;poster.rect=lambda o:o._rect
        poster.showing.return_value=True
        feed=SimpleNamespace(poster=poster)
        with patch('bridge.time.sleep'):
            bridge._open_interaction_panel(feed,ui)
        self.assertEqual(pointer.taps,[(453,145)])


if __name__=='__main__':unittest.main()
