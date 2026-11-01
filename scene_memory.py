"""Conservative source-turn metadata; never infer an agreement or a personal fact."""
import datetime as dt
import re


SHANGHAI = dt.timezone(dt.timedelta(hours=8))
EVENT = r'碰面|见面|聚餐|聚会|开会|会议|面试|考试|出差|旅行|集合|活动'


def dated_expiry(text, stamp):
    day = dt.datetime.fromtimestamp(stamp, SHANGHAI).replace(hour=0, minute=0, second=0, microsecond=0)
    exact = re.search(r'(?:(\d{4})年)?(\d{1,2})月(\d{1,2})[日号]', text)
    iso = re.search(r'(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)', text)
    if exact or iso:
        year, month, date = (exact or iso).groups()
        try:
            target = day.replace(year=int(year) if year else day.year, month=int(month), day=int(date))
        except ValueError:
            return None, ''
        return (target+dt.timedelta(days=1)).timestamp(), '明确日期当天结束'
    week = re.search(r'(这|本|下下|下)(?:周|星期)([一二三四五六日天])?', text)
    if week:
        offset = {'这': 0, '本': 0, '下': 1, '下下': 2}[week.group(1)]
        weekday = '一二三四五六日'.find(week.group(2).replace('天', '日')) if week.group(2) else 6
        target = day+dt.timedelta(days=offset*7-day.weekday()+weekday+1)
        return target.timestamp(), '明确周范围结束'
    relative = re.search(r'大后天|后天|明天|明早|明晚|今天|今日|今晚', text)
    if relative:
        days = {'大后天': 3, '后天': 2, '明天': 1, '明早': 1, '明晚': 1}.get(relative.group(), 0)
        return (day+dt.timedelta(days=days+1)).timestamp(), '相对日期当天结束'
    if re.search(r'最近|近期|这几天|这阵子|这段时间|暂时|目前|现在', text) or re.match(r'^我(?:正在|在|暂住|准备|要出差|要面试|要考试|忙|没空)', text):
        # shortcut: unspecific temporary statements expire after 14 days; add reviewed date forms as needed.
        return stamp+14*86400, '未给结束日，保守保留14天（不是用户约定的期限）'
    return None, ''


def turn_metadata(text, stamp):
    expires, rule = dated_expiry(text, stamp)
    question = bool(re.search(r'[？?]|(?:谁|哪儿|哪里|在哪|几点|多少|为什么|怎么|是否|有没有).*(?:呢|吗|么)$', text))
    event = re.search(EVENT, text)
    kind = 'question' if question else 'arrangement' if event else 'temporary' if expires else 'topic'
    slot, replaces = '', 0
    self_text = re.sub(r'^记住[，,:： ]*', '', text)
    if not question and expires and re.match(r'^我(?:这周|本周|下周|今天|明天|最近|近期|暂时|目前|现在)?(?:在|住在|搬到)', self_text):
        slot, replaces = 'personal:location', 1
    elif not question and event:
        slot = 'event:'+event.group()
        replaces = int(bool(re.search(r'改成|改为|改到|更正|更新|取消|不再|不用|不去了|延期|结束了|完成了', text)))
    return kind, slot, replaces, expires, rule


def source_time(stamp):
    return dt.datetime.fromtimestamp(stamp, SHANGHAI).strftime('%Y-%m-%d %H:%M')+' +08:00'


def query_kind(query):
    if re.search(r'未回|没回|未答|没答|未解决|待回答|漏.*问题', query):
        return 'question'
    if re.search(r'安排|约定|计划', query):
        return 'arrangement'
    return ''


def supersedes(previous, previous_stamp, current, current_stamp):
    old_kind, old_slot, _, old_end, old_rule = turn_metadata(previous, previous_stamp)
    _, slot, replaces, end, rule = turn_metadata(current, current_stamp)
    if not replaces or not slot or slot != old_slot:
        return False
    if slot == 'personal:location':
        return True
    def anchors(text):
        text=re.sub(EVENT, '', text)
        text=re.sub(r'(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|\d{4}-\d{1,2}-\d{1,2}|(?:这|本|下下|下)?(?:周|星期)[一二三四五六日天]?|大后天|后天|明天|今天|今日|今晚|最近|近期|暂时|目前|现在', '', text)
        text=re.sub(r'上午|下午|晚上|早上|中午|[零一二三四五六七八九十\d]+(?:点半?|时|分)|原定|原来|改成|改为|改到|更正|更新|取消|不再|不用|不去了|延期|结束|完成|我们|你们|他们', '', text)
        text=re.sub(r'[我的在到了是把就还再要，。！？!?\s]', '', text)
        return {text[i:i+2] for i in range(len(text)-1)}
    old_anchor, anchor=anchors(previous), anchors(current)
    shared=bool(old_anchor & anchor)
    dated=old_end is not None and end is not None and not old_rule.startswith('未给') and not rule.startswith('未给')
    if dated and old_end != end:
        return shared and bool(re.search(r'改成|改为|改到|延期', current))
    # Distinct named topics can contain the same event noun and even share a day.
    if old_anchor and anchor and not shared:
        return False
    return shared or dated
