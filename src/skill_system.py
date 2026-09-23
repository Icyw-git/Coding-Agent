"""技能系统：SKILL_REGISTRY 扫描 / 列表 / 加载。SKILLS_DIR 由 harness 持有。
"""
from agent_core import _parse_frontmatter


def _h():
    import harness
    return harness


SKILL_REGISTRY:dict[str,dict]={}

def scan_skills():
    if not _h().SKILLS_DIR.exists():
        return
    for d in sorted(_h().SKILLS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest=d / "skill.md"
        if manifest.exists():
            raw=manifest.read_text(encoding='utf-8', errors='replace')
            meta,body=_parse_frontmatter(raw)
            name=meta.get('name',d.name)
            desc=meta.get('description',raw.split('\n')[0].lstrip('#').strip())
            SKILL_REGISTRY[name]={'name':name,
                'description':desc,
                'body':body,
            }


def list_skills()->str:
    if not SKILL_REGISTRY:
        return "No skills registered"
    return '\n'.join(f'- **{skill["name"]}**: {skill["description"]}' for skill in SKILL_REGISTRY.values())

def load_skill(name:str)->str:
    skill=SKILL_REGISTRY.get(name)
    if not skill:
        return f"Error: skill {name} not found"
    return skill['body']
