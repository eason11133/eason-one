from flask import session

TRANSLATIONS = {
    "en": {
        "nav.command": "Command",
        "nav.work": "Work",
        "nav.team": "Team",
        "nav.company": "Company",
        "nav.ceo": "CEO",
        "nav.projects": "Projects",
        "nav.meetings": "Meetings",
        "nav.employees": "Employees",
        "nav.inbox": "Founder Inbox",
        "nav.costs": "Cost Control",
        "nav.models": "Models",
        "lang.en": "EN",
        "lang.zh": "ZH",
        "button.execute": "Execute CEO Request",
        "button.interview": "Interview Employee",
    },
    "zh-TW": {
        "nav.command": "指揮中心",
        "nav.work": "工作",
        "nav.team": "團隊",
        "nav.company": "公司",
        "nav.ceo": "CEO",
        "nav.projects": "專案",
        "nav.meetings": "會議",
        "nav.employees": "員工",
        "nav.inbox": "創辦人收件匣",
        "nav.costs": "成本控制",
        "nav.models": "模型",
        "lang.en": "EN",
        "lang.zh": "中",
        "button.execute": "執行 CEO 請求",
        "button.interview": "訪談員工",
    },
}


def translate(key, language=None):
    lang = language or session.get("language", "en")
    return TRANSLATIONS.get(lang, TRANSLATIONS["en"]).get(
        key, TRANSLATIONS["en"].get(key, key)
    )
