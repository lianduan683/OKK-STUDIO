"""One native window hosting a movable group of the original Tk panels."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk

from native_panel_host import NativePanelHost


class FloatingDockGroup:
    def __init__(self,manager,group):
        self.manager=manager;self.group=group;self.members={};self.pages={};self._after=None;self._closing=False;self._syncing=False
        self._visible_key=None;self._visible_size=None
        self.window=tk.Toplevel(manager.workspace);self.window.withdraw()
        self.window.title('面板组 · OKK STUDIO');self.window.configure(bg='#292b2e')
        self.header=tk.Frame(self.window,bg='#292b2e',cursor='fleur')
        self.header.pack(fill='x')
        self.label=tk.Label(self.header,text='面板组  ⋮⋮  拖动整组停靠',bg='#292b2e',fg='#dedfe3',anchor='w')
        self.label.pack(side='left',padx=manager._px(8),pady=manager._px(3))
        ttk.Button(self.header,text='整组停靠',style='Dock.TButton',command=self.return_home).pack(side='right')
        self.notebook=ttk.Notebook(self.window,style='Dock.TNotebook')
        self.notebook.pack(fill='both',expand=True)
        self.window.protocol('WM_DELETE_WINDOW',self.return_home)
        self.notebook.bind('<<NotebookTabChanged>>',self._selection_changed,add='+')
        self.notebook.bind('<ButtonPress-1>',self._tab_press,add='+')
        self.notebook.bind('<B1-Motion>',manager._motion,add='+')
        self.notebook.bind('<ButtonRelease-1>',manager._release,add='+')
        self.window.bind('<Configure>',self._configure,add='+')
        self.window.bind('<Escape>',manager._cancel_drag,add='+')
        manager._bind_header(self.header,lambda:self.group['tabs'][0],whole_group=True)
        self.window.geometry(group['geometry'])

    def _tab_press(self,event):
        try:page=str(self.notebook.tabs()[self.notebook.index(f'@{event.x},{event.y}')])
        except (tk.TclError,IndexError):return
        key=next((key for key,widget in self.pages.items() if str(widget)==page),None)
        if key is not None:self.manager._press(event,key)

    def _configure(self,event=None):
        # Toplevel bindings also receive descendants' Configure events. A
        # native child resize must not schedule another resize of its parent.
        page=self.pages.get(self.group.get('selected'))
        if event is not None and event.widget is not self.window and event.widget is not page:return
        if not self._closing and not self._syncing and self._after is None:
            self._after=self.window.after_idle(self._layout)

    def _layout(self):
        self._after=None
        if self._closing or self._syncing:return
        selected=self.group['selected'];host=self.members.get(selected);page=self.pages.get(selected)
        if host is None or page is None or not host.attached:return
        size=(max(1,page.winfo_width()),max(1,page.winfo_height()))
        if self._visible_key!=selected:
            previous=self.members.get(self._visible_key)
            if previous is not None and previous.attached:previous.hide()
            host.show()
            self._visible_key=selected;self._visible_size=size
        elif self._visible_size!=size:
            host.resize(*size);self._visible_size=size

    def _selection_changed(self,event=None):
        if self._closing or self._syncing:return
        path=str(self.notebook.select())
        key=next((key for key,page in self.pages.items() if str(page)==path),None)
        if key is None:return
        changed=self.group.get('selected')!=key
        self.group['selected']=key
        self.window.title(self.manager.panels[key].title+' · 面板组 · OKK STUDIO')
        self._configure()
        if changed:self.manager._changed()

    def sync(self,group):
        self.group=group
        self._syncing=True
        try:
            for key in group['tabs']:
                if key not in self.members:
                    panel=self.manager.panels[key]
                    self.manager._detach(panel.widget);self.manager._remove_grip(panel)
                    page=tk.Frame(self.notebook,bg='#18191b')
                    self.pages[key]=page;self.notebook.add(page,text=panel.title)
                    page.bind('<Configure>',self._configure,add='+')
                    host=NativePanelHost(panel.widget,page)
                    # Ownership is recorded before attach, including partial failures.
                    self.members[key]=host
                    host.attach()
                self.manager.panels[key].location='floating'
            for index,key in enumerate(group['tabs']):self.notebook.insert(index,self.pages[key])
            self.window.minsize(max(self.manager._px(self.manager.panels[key].minsize[0]) for key in group['tabs']),
                                max(self.manager._px(self.manager.panels[key].minsize[1]+56) for key in group['tabs']))
        finally:self._syncing=False
        self.select(group['selected'])
        if self.manager.workspace.winfo_viewable():self.window.deiconify()
        else:self.window.withdraw()

    def select(self,key):
        if key not in self.pages:return
        self.group['selected']=key
        self.notebook.select(self.pages[key]);self._selection_changed();self._configure()

    def detach(self,key):
        host=self.members.get(key)
        if host is None:return
        host.detach()
        if getattr(host,'owns_window',False):raise RuntimeError('浮动面板尚未安全移回，保留窗口等待重试。')
        self.members.pop(key,None)
        if self._visible_key==key:self._visible_key=None;self._visible_size=None
        page=self.pages.pop(key,None)
        if page is not None:
            self.notebook.forget(page);page.destroy()

    def return_home(self):
        if not self._closing and self.group['tabs']:
            self.manager.return_group_home(self.group['tabs'][0])

    def close(self):
        self._closing=True
        if self._after is not None:
            self.window.after_cancel(self._after);self._after=None
        errors=[]
        for key in list(self.members):
            try:self.detach(key)
            except Exception as error:errors.append(error)
        if errors:
            self._closing=False
            raise errors[0]
        self.window.destroy()
