"""验收辅助脚本自身的回归：卡片校验必须真的能发现结构问题。"""
import copy
import json

from scripts import feishu_acceptance as acc


def test_every_card_passes_structure_validation():
    cards = acc.build_cards()
    assert set(cards) >= {'控制台', '定时任务', '定时任务·删除确认', '会话列表',
                          '工作区选择'}
    for name, card in cards.items():
        require = not name.startswith('回复卡')
        assert acc.validate_card(name, card, require_buttons=require) == [], name


def test_reply_cards_use_schema_2_body_elements():
    card = acc.build_cards()['回复卡·完成']
    assert card['schema'] == '2.0'
    assert card['body']['elements']


def test_validate_card_flags_oversized_payload():
    card = acc.build_cards()['控制台']
    problems = acc.validate_card('控制台', card, max_bytes=16)
    assert any('上限' in item for item in problems)


def test_validate_card_flags_button_without_value_and_missing_buttons():
    card = acc.build_cards()['定时任务']
    broken = copy.deepcopy(card)
    button = next(el for el in broken['elements']
                  if el.get('tag') == 'action')['actions'][0]
    button['value'] = {}
    assert any('没有 value' in item
               for item in acc.validate_card('定时任务', broken))

    stripped = copy.deepcopy(card)
    stripped['elements'] = [el for el in stripped['elements']
                            if el.get('tag') != 'action']
    assert any('没有任何按钮' in item
               for item in acc.validate_card('定时任务', stripped))


def test_validate_card_flags_unknown_tag_and_empty_content():
    card = {'elements': [{'tag': 'mystery'}, {'tag': 'div',
                                             'text': {'tag': 'plain_text',
                                                      'content': ''}}]}
    problems = acc.validate_card('x', card, require_buttons=False)
    assert any('未知元素' in item for item in problems)
    assert any('div 内容为空' in item for item in problems)


def test_validate_card_reports_json_failure():
    problems = acc.validate_card('x', {'elements': [{'tag': 'div',
                                                     'text': {'content': object()}}]})
    assert problems and 'JSON' in problems[0]


def test_check_env_reports_missing_credentials():
    assert acc.check_env({}) == [
        '缺少 FEISHU_APP_ID', '缺少 FEISHU_APP_SECRET', '缺少 HARNESS_API_TOKEN',
        '缺少 FEISHU_ALLOWED_USERS（或显式 FEISHU_ALLOW_ANY_USER=1）']
    assert acc.check_env({'FEISHU_APP_ID': 'a', 'FEISHU_APP_SECRET': 'b',
                          'HARNESS_API_TOKEN': 'c',
                          'FEISHU_ALLOWED_USERS': 'ou_x'}) == []
    assert acc.check_env({'FEISHU_APP_ID': 'a', 'FEISHU_APP_SECRET': 'b',
                          'HARNESS_API_TOKEN': 'c',
                          'FEISHU_ALLOW_ANY_USER': '1'}) == []


def test_cards_stay_under_feishu_size_limit():
    for name, card in acc.build_cards().items():
        size = len(json.dumps(card, ensure_ascii=False).encode('utf-8'))
        assert size < acc.MAX_CARD_BYTES, name
