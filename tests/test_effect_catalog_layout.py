"""Catalog identity, filtering, and real Tk dock/floating-size regression checks."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from effects_browser import EffectsBrowser, filtered_presets, library_presets
from effects_catalog import catalog_categories, mask_metadata, overlay_metadata
from effects_library import OverlayAsset, TransitionMask


def overlay(identifier='tpl30_mov08', name='long thin blue green', category='其他'):
    return OverlayAsset(identifier,name,'alpha',category,identifier+'.mov',1920,1080,30,90,3,'模板合集')


class EffectCatalogTests(unittest.TestCase):
    def test_known_assets_gain_categories_without_changing_saved_identity(self):
        assets=[overlay(),overlay('tpl01_seq02','模板01·2图层'),overlay('unknown','未识别素材')]
        masks=[TransitionMask('mask204','15款漏光转场 Light Leak01 Light Leak 01','15款漏光转场 Light Leak','a.png'),
               TransitionMask('mask083','光线泄漏01 Light Leak 01','光线泄漏','b.png'),
               TransitionMask('mask021','Comic 0201 Texture 2','Comic 02','c.png')]
        with patch('effects_browser.load_overlay_assets',return_value=assets), \
             patch('effects_browser.load_transition_masks',return_value=masks):
            catalog=library_presets()
        by_key={item.key:item for item in catalog}
        self.assertEqual(by_key['overlay:tpl30_mov08'].name,'蓝绿色细长条')
        self.assertEqual(by_key['overlay:tpl30_mov08'].category,'叠加 / 图形')
        self.assertEqual(by_key['overlay:tpl01_seq02'].category,'叠加 / 模板图层')
        self.assertEqual(by_key['overlay:unknown'].category,'叠加 / 其他素材')
        for asset in assets:self.assertIs(by_key['overlay:'+asset.id].asset,asset)
        for mask in masks:self.assertIs(by_key['mask:'+mask.id].asset,mask)
        self.assertEqual(by_key['mask:mask204'].name,'漏光 A01')
        self.assertEqual(by_key['mask:mask083'].name,'漏光 B01')
        self.assertEqual(by_key['mask:mask021'].category,'遮罩转场 / 漫画纹理')
        self.assertEqual(len(filtered_presets('遮罩转场 / 漏光',presets=catalog)),2)
        self.assertEqual(filtered_presets('叠加','BLUE green',catalog),(by_key['overlay:tpl30_mov08'],))
        self.assertEqual(filtered_presets('叠加','蓝绿 细长条',catalog),(by_key['overlay:tpl30_mov08'],))
        self.assertEqual(filtered_presets('遮罩转场','Ｔｅｘｔｕｒｅ 2',catalog),(by_key['mask:mask021'],))
        self.assertEqual(filtered_presets('叠加','tpl01_seq02',catalog),(by_key['overlay:tpl01_seq02'],))
        self.assertEqual(catalog_categories(catalog,'叠加'),
            ('叠加 / 图形','叠加 / 模板图层','叠加 / 其他素材'))

    def test_bundled_catalog_has_unique_keys_and_no_missing_asset_inventions(self):
        from effects_library import load_overlay_assets,load_transition_masks
        overlays,masks=load_overlay_assets(),load_transition_masks()
        catalog=library_presets()
        self.assertEqual(len({item.key for item in catalog}),len(catalog))
        self.assertEqual({item.key[8:] for item in catalog if item.kind=='overlay'},{asset.id for asset in overlays})
        self.assertEqual({item.key[5:] for item in catalog if item.kind=='mask'},{asset.id for asset in masks})
        self.assertTrue(all('其他' != item.category for item in catalog))


class EffectCatalogLayoutTests(unittest.TestCase):
    def setUp(self):
        try:self.root=tk.Tk()
        except tk.TclError as error:self.skipTest(str(error))
        self.root.withdraw();self.root.attributes('-alpha',0)
        self.root.tk.call('tk','scaling',96/72)
        self.errors=[]
        self.root.report_callback_exception=lambda *error:self.errors.append(error)
        self.workspace=SimpleNamespace(_px=lambda value:value,selected_program_range=None,
            _available=lambda:True,clear_grade_preview=Mock(),
            apply_library_overlay=Mock(),apply_library_mask=Mock())
        self.thumbnail_patch=patch('effects_browser.EffectsBrowser._request_thumbnail')
        self.thumbnail_patch.start()
        ttk.Style(self.root).configure('Multicam.Treeview',rowheight=25)
        self.panel=EffectsBrowser(self.root,self.workspace)
        self.panel.pack(fill='both',expand=True)

    def tearDown(self):
        self.panel.destroy();self.root.destroy();self.thumbnail_patch.stop()

    def resize(self,width,height):
        self.root.geometry(f'{width}x{height}+10000+10000');self.root.deiconify();self.root.update()

    def test_narrow_inspector_and_wide_floating_sizes_keep_controls_inside(self):
        for width,height in ((260,380),(304,340),(460,400),(900,560),(304,340)):
            with self.subTest(width=width,height=height):
                self.resize(width,height)
                for widget in (self.panel.categories,self.panel.subcategories,self.panel.apply_button,self.panel.preview_button):
                    left=widget.winfo_rootx()-self.panel.winfo_rootx()
                    top=widget.winfo_rooty()-self.panel.winfo_rooty()
                    self.assertGreaterEqual(left,0)
                    self.assertLessEqual(left+widget.winfo_width(),width)
                    self.assertGreaterEqual(top,0)
                    self.assertLessEqual(top+widget.winfo_height(),height)
                self.assertGreaterEqual(sum(bool(self.panel.list.bbox(key)) for key in self.panel.list.get_children()),4)
                self.assertEqual(bool(self.panel.list['displaycolumns']),width>=430)
        self.assertEqual(self.errors,[])

    def test_categories_search_and_reload_preserve_selected_asset(self):
        self.resize(304,400)
        self.panel.category.set('叠加')
        self.panel.subcategory.set('图形')
        self.panel.query.set('blue green')
        self.assertIn('overlay:tpl30_mov08',self.panel.list.get_children())
        self.panel.list.selection_set('overlay:tpl30_mov08');self.panel._selection_changed()
        self.panel.reload_library();self.root.update()
        self.assertEqual(self.panel.selected_preset().key,'overlay:tpl30_mov08')
        self.assertIn('叠加 / 图形',self.panel.category_text.get())
        self.panel.category.set('遮罩转场');self.panel.query.set('');self.root.update()
        self.assertEqual(self.panel.subcategory.get(),'全部类型')
        self.assertTrue(all(item.kind=='mask' for item in self.panel._items))
        self.panel.subcategory.set('漏光')
        self.assertEqual(len(self.panel._items),30)
        self.panel.query.set('不存在的素材名')
        self.assertFalse(self.panel.list.get_children())
        self.assertEqual(str(self.panel.apply_button['state']),'disabled')
        self.assertEqual(self.errors,[])

    def test_thumbnail_recenters_on_dock_resize_without_reading_asset_again(self):
        self.resize(460,400)
        self.panel.thumbnail.pack(fill='x')
        self.panel._thumb_original=tk.PhotoImage(master=self.root,width=256,height=144)
        for width in (460,260,800):
            self.resize(width,400)
            self.panel._draw_thumbnail()
            picture=self.panel.thumbnail.find_all()[0]
            x,y=self.panel.thumbnail.coords(picture)
            self.assertAlmostEqual(x,self.panel.thumbnail.winfo_width()/2)
            self.assertLessEqual(self.panel._thumb_photo.width(),self.panel.thumbnail.winfo_width())
        self.assertEqual(self.errors,[])

    def test_high_dpi_inspector_keeps_primary_action_and_filters_visible(self):
        self.panel.destroy()
        self.root.tk.call('tk','scaling',144/72)
        self.workspace._px=lambda value:round(value*1.5)
        ttk.Style(self.root).configure('Multicam.Treeview',rowheight=38)
        self.panel=EffectsBrowser(self.root,self.workspace)
        self.panel.pack(fill='both',expand=True)
        self.resize(456,540)
        for widget in (self.panel.apply_button,self.panel.categories,self.panel.subcategories):
            self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.panel.winfo_rootx()+456)
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),self.panel.winfo_rooty()+540)
        self.assertGreaterEqual(sum(bool(self.panel.list.bbox(key)) for key in self.panel.list.get_children()),4)
        self.assertEqual(self.errors,[])


if __name__=='__main__':unittest.main()
