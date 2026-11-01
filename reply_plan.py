"""Keep a chosen reply and sticker together when a queued model result survives restart."""
import json,re
PREFIX='\x1ereply-plan-v1:'

def _validate(plan):
    required={'body','structured_reply','sticker_choice','sticker_only'}
    if not isinstance(plan,dict) or not required<=set(plan) or set(plan)-required-{'semantic_sources'}:
        raise ValueError('invalid_reply_plan')
    if not isinstance(plan['body'],str) or not plan['body'] or len(plan['body'])>1200:
        raise ValueError('invalid_reply_body')
    if type(plan['structured_reply']) is not bool or type(plan['sticker_only']) is not bool:
        raise ValueError('invalid_reply_mode')
    choice=plan['sticker_choice']
    if choice is not None and (not isinstance(choice,str) or not re.fullmatch('[a-f0-9]{64}',choice)):
        raise ValueError('invalid_sticker_choice')
    if plan['sticker_only'] and (not plan['structured_reply'] or choice is None):
        raise ValueError('invalid_sticker_only_plan')
    sources=plan.get('semantic_sources',[])
    if not isinstance(sources,list) or len(sources)>4 or any(not isinstance(value,str) or not re.fullmatch('[a-f0-9]{64}',value) for value in sources):
        raise ValueError('invalid_semantic_sources')
    return plan

def freeze_reply(job,reply):
    plan=dict(body=reply,structured_reply=bool(job.get('structured_reply')),sticker_choice=job.get('sticker_choice'),sticker_only=bool(job.get('sticker_only')))
    if job.get('semantic_sources'):plan['semantic_sources']=job['semantic_sources']
    return PREFIX+json.dumps(_validate(plan),ensure_ascii=False,separators=(',',':'))

def restore_reply(job,cached):
    if not cached or not cached.startswith(PREFIX):return cached
    try:plan=_validate(json.loads(cached[len(PREFIX):]))
    except (ValueError,TypeError) as exc:raise ValueError('invalid_cached_reply_plan') from exc
    job.update({key:plan[key] for key in ('structured_reply','sticker_choice','sticker_only')})
    if 'semantic_sources' in plan:job['semantic_sources']=plan['semantic_sources']
    return plan['body']
