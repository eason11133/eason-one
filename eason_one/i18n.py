from flask import session
TRANSLATIONS={
 "en":{"nav.ceo":"CEO","nav.projects":"Projects","nav.meetings":"Meetings","nav.employees":"Employees","nav.inbox":"Founder Inbox","nav.costs":"Cost Control","nav.models":"Models","lang.en":"EN","lang.zh":"繁中","button.execute":"Execute CEO Request","button.interview":"Interview Employee"},
 "zh-TW":{"nav.ceo":"CEO","nav.projects":"專案","nav.meetings":"會議","nav.employees":"員工","nav.inbox":"創辦人收件匣","nav.costs":"成本控制","nav.models":"模型","lang.en":"EN","lang.zh":"繁中","button.execute":"執行 CEO 指令","button.interview":"訪談員工"}
}
def translate(key,language=None):
    lang=language or session.get("language","en")
    return TRANSLATIONS.get(lang,TRANSLATIONS["en"]).get(key,TRANSLATIONS["en"].get(key,key))
