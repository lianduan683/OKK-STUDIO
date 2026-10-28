"""Presentation metadata for shipped effects; saved asset identities stay intact."""
from __future__ import annotations

import re
import unicodedata


GROUPS = ('全部', '调色风格', '转场', '叠加', '遮罩转场', '字幕工具', '防抖工具')
OVERLAY_CATEGORIES = ('光效', '粒子', '图形', '字幕动画', '模板图层', '氛围', '纹理', '遮罩', '转场素材', '其他素材')
MASK_CATEGORIES = ('画笔', '墨水', '喷洒', '形状擦除', '分割', '漏光', '闪光', '火焰', '烟雾',
                   '故障', '漫画', '漫画纹理', '万花筒', '优雅', '幻灯片', '排版', '相机', '缩放',
                   '伸展', '扭曲', '视差', '透视')

# These names translate the shipped manifest, rather than adding new effects.
OVERLAY_NAMES = {
    'tpl30_mov01': '红色小矩形', 'tpl30_mov02': '蓝绿色小矩形',
    'tpl30_mov03': '白色小球 · 左侧', 'tpl30_mov04': '圆点',
    'tpl30_mov05': '蓝色长条', 'tpl30_mov06': '紫色长条',
    'tpl30_mov07': '黄橙色长条', 'tpl30_mov08': '蓝绿色细长条',
    'tpl30_mov09': '红色细长条', 'tpl30_mov10': '白色长条',
    'tpl30_mov11': '五个小球', 'tpl30_mov12': '白色小球 · 上方',
    'tpl30_mov13': '白色小块', 'tpl30_mov14': '细块 · 左侧',
    'tpl05_mov01': '文字粒子 01', 'tpl10_mov01': '发光粒子',
    'tpl10_mov02': '粒子 2.2', 'tpl27_mov01': '开场粒子',
    'tpl49_mov01': '形状图层 01', 'tpl49_mov02': '粒子 · 模板49',
}


def overlay_metadata(asset):
    name = OVERLAY_NAMES.get(asset.id, asset.name)
    category = asset.category
    sequence = re.fullmatch(r'tpl(\d+)_seq(\d+)', asset.id)
    if sequence:
        name = f'模板 {int(sequence[1]):02d} · 图层 {int(sequence[2]):02d}'
        category = '模板图层'
    elif asset.id.startswith('tpl30_') or asset.id in ('tpl47_mov01', 'tpl49_mov01'):
        category = '图形'
    elif asset.id == 'tpl49_mov02':
        category = '粒子'
    elif category == '其他':
        category = '转场素材' if '转场' in name else '其他素材'
    if category == '字幕动画' and re.match(r'总?合成', name):
        template = re.match(r'tpl(\d+)', asset.id)
        if template: name = f'字幕动画 · 模板 {int(template[1]):02d}'
    return name, '叠加 / ' + category


def mask_metadata(mask):
    category = mask.category
    if category in ('15款漏光转场 Light Leak', '光线泄漏'):
        family = 'A' if category.startswith('15款') else 'B'
        number = re.search(r'(\d+)\s*$', mask.name)
        name = f'漏光 {family}{int(number[1]):02d}' if number else mask.name
        return name, '遮罩转场 / 漏光'
    if category.startswith('Comic '):
        family = re.search(r'\d+', category)
        name = f'漫画纹理 {int(family[0]):02d}' if family else mask.name
        return name, '遮罩转场 / 漫画纹理'
    # Keep the original name searchable, but remove its repeated English title
    # from the narrow list. The numbered Chinese name identifies the asset.
    match = re.match(r'^([^\d]+?)(\d+)\s+', mask.name)
    name = f'{match[1].strip()} {int(match[2]):02d}' if match else mask.name
    return name, '遮罩转场 / ' + category


def category_sort_key(category):
    group, _, detail = category.partition(' / ')
    order = OVERLAY_CATEGORIES if group == '叠加' else MASK_CATEGORIES
    return (GROUPS.index(group) if group in GROUPS else len(GROUPS),
            order.index(detail) if detail in order else len(order), detail)


def catalog_categories(presets, group='全部'):
    """Only offer categories backed by actual loaded presets."""
    values = {item.category for item in presets if ' / ' in item.category
              and (group == '全部' or item.category.startswith(group + ' / '))}
    return tuple(sorted(values, key=category_sort_key))


def searchable_text(item):
    asset = item.asset
    original = ' '.join(str(getattr(asset, key, '')) for key in ('id', 'name', 'category', 'source', 'file'))
    text = f'{item.name} {item.category} {item.description} {item.key} {original}'
    return unicodedata.normalize('NFKC', text).casefold()


def query_words(query):
    return unicodedata.normalize('NFKC', str(query)).casefold().split()
