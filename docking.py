"""Movable Tk work panels without replacing their native video windows.

Panels must be classic ``tk.Frame`` children of one common workspace.  The
workspace's existing grid/pack/paned/notebook layout is the default home.  New
docking groups are sibling containers, so moving a panel never destroys its
children. Single panels use ``wm manage``. On Windows, grouped floating panels
retain that managed native identity and embed only their wrapper in one shell.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import json
import math
import os
from pathlib import Path
import re
import tkinter as tk
from tkinter import ttk

PANEL='#292b2e'; BORDER='#18191b'; TEXT='#dedfe3'; ACCENT='#58a6d6'


@dataclass
class _Home:
    manager: str
    container: tk.Misc
    options: dict=field(default_factory=dict)
    index: int=0


@dataclass
class DockPanel:
    key: str
    title: str
    widget: tk.Frame
    home: _Home
    header: tk.Misc | None=None
    minsize: tuple=(220,140)
    location: str='home'
    grip: tk.Misc | None=None
    grid_shift: tuple | None=None
    close_command: str | None=None


def _walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


class DockManager:
    """Register existing leaf panels, then drag a header or notebook tab.

    ``on_focus(anchor_key)`` may reveal a collapsed external sidebar before a
    docked panel is selected. ``on_change()`` is called after completed changes.
    Minimum sizes are logical pixels; saved split ratios survive DPI changes.
    """
    VERSION=2

    def __init__(self,workspace,*,scale=None,on_change=None,on_focus=None):
        self.workspace=workspace
        self.scale=float(scale(100)/100 if callable(scale) else (scale or max(1.,workspace.winfo_fpixels('1i')/96.)))
        self.on_change=on_change;self.on_focus=on_focus
        self.panels={};self._anchors={};self._roots={};self._floating={};self._hidden={}
        self._floating_groups={};self._float_hosts={};self._home_headers={}
        self._default_roots={};self._active=False;self._counter=0
        self._home_anchors={};self._default_hidden={}
        self._grid_defaults={}
        self._containers=[];self._groups={};self._sashes=[];self._bound={}
        self._drag=None;self._overlay=None;self._closed=False;self._after=None
        self._binding_ids=[]
        for event in ('<Map>','<Unmap>'):
            identity=workspace.bind(event,self._workspace_map,add='+')
            self._binding_ids.append((workspace,event,identity))
        style=ttk.Style(workspace)
        style.configure('Dock.TNotebook',background=PANEL,borderwidth=0)
        style.configure('Dock.TNotebook.Tab',padding=(self._px(10),self._px(5)))

    def _px(self,value):return max(1,round(value*self.scale))

    def _capture_home(self,widget):
        manager=widget.winfo_manager()
        if manager in ('pack','grid','place'):
            info=getattr(widget,manager+'_info')()
            container=info.pop('in')
            siblings=getattr(container,manager+'_slaves')()
            return _Home(manager,container,info,siblings.index(widget))
        for container in _walk(self.workspace):
            if isinstance(container,ttk.Notebook) and str(widget) in container.tabs():
                return _Home('notebook',container,dict(container.tab(widget)),container.index(widget))
            if isinstance(container,(tk.PanedWindow,ttk.Panedwindow)) and str(widget) in tuple(map(str,container.panes())):
                if isinstance(container,ttk.Panedwindow):options=dict(container.pane(widget))
                else:options={key:value[-1] for key,value in container.paneconfigure(widget).items()}
                return _Home('panedwindow',container,options,list(map(str,container.panes())).index(str(widget)))
        raise ValueError('面板必须先放入默认布局，或显式指定 home。')

    def register(self,key,title,widget,*,home=None,header=None,minsize=(220,140),default_hidden=False):
        if key in self.panels:raise ValueError('重复面板标识：'+str(key))
        if widget.master is not self.workspace or widget.winfo_class()!='Frame':
            raise ValueError('可停靠面板必须是共同 workspace 的直接 tk.Frame 子控件。')
        if home is None:home=self._capture_home(widget)
        elif isinstance(home,dict):
            default_hidden=bool(home.get('hidden',default_hidden))
            home=_Home(home['manager'],home['container'],dict(home.get('options',{})),int(home.get('index',0)))
        if not isinstance(home,_Home):raise TypeError('Invalid panel home')
        if home.manager=='paned':home.manager='panedwindow'
        panel=DockPanel(str(key),str(title),widget,home,header,tuple(map(int,minsize)))
        self.panels[key]=panel
        if home.manager=='notebook':
            anchor=next((name for name,entry in self._anchors.items() if entry['original'] is home.container),None)
            if anchor is None:
                anchor=key;self._anchors[anchor]={'original':home.container,'home':self._capture_home(home.container)}
                self._roots[anchor]=self._group([])
                self._make_home_group_header(anchor,home.container)
            self._roots[anchor]['tabs'].append(key)
            self._roots[anchor]['tabs'].sort(key=lambda item:self.panels[item].home.index)
            selected=str(home.container.select())
            if selected==str(widget):self._roots[anchor]['selected']=key
            self._bind_notebook(home.container)
        else:
            anchor=key;self._anchors[anchor]={'original':widget,'home':home}
            self._roots[anchor]=self._group([key])
        self._home_anchors[key]=anchor
        anchor_home=self._anchors[anchor]['home']
        if anchor_home.manager=='grid':
            for axis in ('row','column'):
                index=int(anchor_home.options.get(axis,0));container=anchor_home.container
                self._grid_defaults.setdefault((container,axis,index),dict(getattr(container,'grid_'+axis+'configure')(index)))
        if default_hidden:
            self._remove(key)
            self._hidden[key]={'anchor':anchor,'neighbor':None,'floating':None}
            self._default_hidden[key]=dict(self._hidden[key])
            self._detach(widget);panel.location='hidden'
        self._default_roots=copy.deepcopy(self._roots)
        if header is not None:self._bind_header(header,key)
        self._bind_alt(widget,key)
        if self._active:self._render()
        return panel

    def _group(self,tabs):
        self._counter+=1
        return {'type':'group','id':f'g{self._counter}','tabs':list(tabs),'selected':tabs[0] if tabs else None}

    def _bind(self,widget,event,callback):
        identity=widget.bind(event,callback,add='+')
        self._binding_ids.append((widget,event,identity))

    def _bind_alt(self,widget,key):
        # Applies to a floating panel as a toplevel, including blank content.
        self._bind(widget,'<Alt-ButtonPress-1>',lambda event:self._press(event,key))
        self._bind(widget,'<Alt-B1-Motion>',self._motion)
        self._bind(widget,'<Alt-ButtonRelease-1>',self._release)
        self._bind(widget,'<Escape>',self._cancel_drag)

    def _bind_header(self,header,key,*,whole_group=False):
        current=lambda:key() if callable(key) else key
        for widget in _walk(header):
            if widget.winfo_class() not in ('Frame','Label','TFrame','TLabel'):continue
            self._bind(widget,'<ButtonPress-1>',lambda event:self._press(event,current(),whole_group=whole_group))
            self._bind(widget,'<B1-Motion>',self._motion)
            self._bind(widget,'<ButtonRelease-1>',self._release)
            self._bind(widget,'<Double-Button-1>',lambda event:self.float_group(current()) if whole_group else self._double(current()))

    def _make_home_group_header(self,anchor,notebook):
        home=self._anchors[anchor]['home']
        if home.manager!='pack':return
        header=tk.Frame(home.container,bg=PANEL,cursor='fleur')
        tk.Label(header,text='面板组  ⋮⋮',bg=PANEL,fg=TEXT,anchor='w').pack(side='left',padx=self._px(6))
        current=lambda:(self._roots.get(anchor) or {}).get('tabs',[anchor])[0]
        ttk.Button(header,text='↗',width=3,style='Dock.TButton',command=lambda:self.float_group(current())).pack(side='right')
        header.pack(side='top',fill='x',before=notebook)
        self._bind_header(header,current,whole_group=True)
        self._home_headers[anchor]=header

    def _bind_notebook(self,notebook):
        if str(notebook) in self._bound:return
        self._bound[str(notebook)]=notebook
        def press(event):
            try:path=str(notebook.tabs()[notebook.index(f'@{event.x},{event.y}')])
            except (tk.TclError,IndexError):return
            key=next((key for key,panel in self.panels.items() if str(panel.widget)==path),None)
            if key is not None:self._press(event,key)
        self._bind(notebook,'<ButtonPress-1>',press)
        self._bind(notebook,'<B1-Motion>',self._motion)
        self._bind(notebook,'<ButtonRelease-1>',self._release)

    def _double(self,key):
        if key in self._floating:self._return_home(key)
        else:self.float_panel(key)
        return 'break'

    def _detach(self,widget):
        manager=widget.winfo_manager()
        if manager=='wm':self.workspace.tk.call('wm','forget',widget)
        elif manager in ('pack','grid','place'):getattr(widget,manager+'_forget')()
        elif manager=='notebook':
            for notebook in tuple(self._bound.values()):
                try:
                    if str(widget) in notebook.tabs():notebook.forget(widget);break
                except tk.TclError:pass
        elif manager=='panedwindow':
            for container in _walk(self.workspace):
                if isinstance(container,(tk.PanedWindow,ttk.Panedwindow)) and str(widget) in tuple(map(str,container.panes())):
                    container.forget(widget);break

    def _mount(self,widget,home):
        options=dict(home.options);container=home.container
        if home.manager=='notebook':
            options.setdefault('text',str(widget))
            index=home.index if home.index<len(container.tabs()) else 'end'
            container.insert(index,widget,**options)
        elif home.manager=='panedwindow':
            if isinstance(container,ttk.Panedwindow):
                index=home.index if home.index<len(container.panes()) else 'end'
                container.insert(index,widget,**options)
            else:
                for key in ('before','after'):options.pop(key,None)
                panes=container.panes()
                if home.index<len(panes):options['before']=panes[home.index]
                container.add(widget,**options)
        elif home.manager=='pack':
            siblings=container.pack_slaves()
            if home.index<len(siblings):options['before']=siblings[home.index]
            widget.pack(in_=container,**options)
        else:getattr(widget,home.manager)(in_=container,**options)

    def _nodes(self,node):
        if not node:return
        yield node
        if node['type']=='split':
            for child in node['children']:yield from self._nodes(child)

    def _find(self,key):
        for anchor,root in self._roots.items():
            for node in self._nodes(root):
                if node['type']=='group' and key in node['tabs']:return anchor,node
        return None,None

    def _floating_group(self,key):
        return next((group for group in self._floating_groups.values() if key in group['tabs']),None)

    def group_members(self,key):
        group=self._floating_group(key) or self._find(key)[1]
        return tuple(group['tabs']) if group is not None else (key,)

    def floating_group_window(self,key):
        group=self._floating_group(key)
        host=self._float_hosts.get(group['id']) if group else None
        return host.window if host else None

    def iter_floating_windows(self):
        yield from (self.panels[key].widget for key in self._floating)
        yield from (host.window for host in self._float_hosts.values())

    iter_float_windows=iter_floating_windows

    def _floating_keys(self):
        return set(self._floating).union(*(set(group['tabs']) for group in self._floating_groups.values()))

    def _remove(self,key):
        def prune(node):
            if node is None:return None
            if node['type']=='group':
                node['tabs']=[item for item in node['tabs'] if item!=key]
                if not node['tabs']:return None
                if node.get('selected') not in node['tabs']:node['selected']=node['tabs'][0]
                return node
            children=[item for child in node['children'] if (item:=prune(child)) is not None]
            if not children:return None
            if len(children)==1:return children[0]
            node['children']=children;return node
        for anchor in self._roots:self._roots[anchor]=prune(self._roots[anchor])
        self._floating.pop(key,None);self._hidden.pop(key,None)
        for identity,group in list(self._floating_groups.items()):
            if key in group['tabs']:
                group['tabs'].remove(key)
                if not group['tabs']:del self._floating_groups[identity]
                elif group['selected']==key:group['selected']=group['tabs'][0]

    def _replace_node(self,anchor,target,replacement):
        if self._roots[anchor] is target:self._roots[anchor]=replacement;return
        for node in self._nodes(self._roots[anchor]):
            if node['type']=='split':
                for index,child in enumerate(node['children']):
                    if child is target:node['children'][index]=replacement;return

    def _capture_ratios(self):
        for key in self._floating:self._floating[key]=self._float_geometry(key)
        for identity,host in self._float_hosts.items():
            if identity in self._floating_groups:
                self._floating_groups[identity]['geometry']=host.window.geometry()
        for node,widget in self._sashes:
            try:
                length=widget.winfo_width() if node['orient']=='horizontal' else widget.winfo_height()
                coordinate=widget.sash_coord(0)[0 if node['orient']=='horizontal' else 1]
                if length>1:node['ratio']=max(.05,min(.95,coordinate/length))
            except tk.TclError:pass
        for node,notebook in self._groups.values():
            try:
                selected=str(notebook.select())
                node['selected']=next((key for key in node['tabs'] if str(self.panels[key].widget)==selected),node.get('selected'))
            except tk.TclError:pass

    def _prepare_float_hosts(self):
        # Native wrappers must leave their host before any placeholder/window
        # can be destroyed; the original content HWNDs belong to the workspace.
        for identity,host in list(self._float_hosts.items()):
            wanted=self._floating_groups.get(identity,{}).get('tabs',[])
            for key in tuple(host.members):
                if key not in wanted:host.detach(key)
            if not wanted:
                host.close();del self._float_hosts[identity]

    def _sync_float_hosts(self):
        if not self._floating_groups:return
        from floating_dock import FloatingDockGroup
        for identity,group in self._floating_groups.items():
            host=self._float_hosts.get(identity)
            if host is None:
                host=FloatingDockGroup(self,group);self._float_hosts[identity]=host
            host.sync(group)

    def _close_float_hosts(self):
        errors=[]
        for identity,host in list(self._float_hosts.items()):
            try:host.close()
            except Exception as error:errors.append(error)
            else:del self._float_hosts[identity]
        if errors:raise errors[0]

    def _clear_runtime(self):
        if self._after is not None:
            try:self.workspace.after_cancel(self._after)
            except tk.TclError:pass
            self._after=None
        for panel in self.panels.values():
            if panel.widget.winfo_manager()!='wm':self._detach(panel.widget)
        for widget in reversed(self._containers):
            try:widget.destroy()
            except tk.TclError:pass
        self._containers=[];self._groups={};self._sashes=[]
        self._bound={key:value for key,value in self._bound.items() if value.winfo_exists()}
        self._binding_ids=[entry for entry in self._binding_ids if entry[0].winfo_exists()]

    def _minimum(self,node):
        if node['type']=='group':
            sizes=[self.panels[key].minsize for key in node['tabs']]
            return max(size[0] for size in sizes),max(size[1] for size in sizes)+28
        a,b=map(self._minimum,node['children'])
        return (a[0]+b[0]+5,max(a[1],b[1])) if node['orient']=='horizontal' else (max(a[0],b[0]),a[1]+b[1]+5)

    def _build_node(self,node):
        if node['type']=='group':
            frame=tk.Frame(self.workspace,bg=PANEL)
            self._containers.append(frame)
            header=tk.Frame(frame,bg=PANEL,cursor='fleur')
            header.pack(fill='x')
            tk.Label(header,text='面板组  ⋮⋮',bg=PANEL,fg=TEXT,anchor='w').pack(side='left',padx=self._px(6))
            ttk.Button(header,text='↗',width=3,style='Dock.TButton',command=lambda key=node['tabs'][0]:self.float_group(key)).pack(side='right')
            self._bind_header(header,node['tabs'][0],whole_group=True)
            notebook=ttk.Notebook(frame,style='Dock.TNotebook')
            notebook.pack(fill='both',expand=True)
            for key in node['tabs']:
                panel=self.panels[key]
                self._detach(panel.widget);self._remove_grip(panel)
                notebook.add(panel.widget,text=panel.title)
                panel.location='group'
            selected=node.get('selected')
            if selected in node['tabs']:notebook.select(self.panels[selected].widget)
            self._bind_notebook(notebook)
            self._groups[node['id']]=(node,notebook)
            return frame
        pane=tk.PanedWindow(self.workspace,orient=node['orient'],bg=BORDER,bd=0,
                            sashwidth=self._px(5),showhandle=False,opaqueresize=True)
        self._containers.append(pane)
        for child in node['children']:
            widget=self._build_node(child)
            minimum=self._minimum(child)[0 if node['orient']=='horizontal' else 1]
            pane.add(widget,minsize=self._px(minimum),stretch='always')
        self._sashes.append((node,pane))
        return pane

    def _render(self):
        self._active=True;self._prepare_float_hosts();self._clear_runtime()
        for header in self._home_headers.values():header.pack_forget()
        for entry in self._anchors.values():
            if entry['original'].winfo_manager()!='wm':self._detach(entry['original'])
        for key,panel in self.panels.items():
            if key not in self._floating_keys():
                self._detach(panel.widget);self._remove_grip(panel)
                panel.location='hidden'
        for anchor,node in self._roots.items():
            if node is not None:self._mount(self._build_node(node),self._anchors[anchor]['home'])
        for key,geometry in self._floating.items():self._manage_float(key,geometry)
        self._sync_float_hosts()
        self._sync_grid_homes()
        def sashes():
            self._after=None
            for node,pane in self._sashes:
                if not pane.winfo_exists():continue
                horizontal=node['orient']=='horizontal'
                length=pane.winfo_width() if horizontal else pane.winfo_height()
                position=round(length*node.get('ratio',.5))
                pane.sash_place(0,position if horizontal else 0,0 if horizontal else position)
            self._raise_panels()
        self._after=self.workspace.after_idle(sashes)

    def _raise_panels(self):
        # Frames keep their common native parent while notebook/paned geometry
        # masters are nested siblings. New containers are higher in that native
        # sibling stack; explicitly raise the original leaves above their hosts.
        for panel in self.panels.values():
            if panel.widget.winfo_manager() not in ('','wm'):
                panel.widget.lift()

    def _changed(self):
        if callable(self.on_change):self.on_change()

    def focus_home(self,key):
        if key in self._floating_keys():return None
        anchor,_=self._find(key)
        if anchor is not None:return anchor
        return self._home_anchors.get(key)

    def panel_location(self,key):return self.panels[key].location
    def home_has_content(self,key):return self._roots.get(self._home_anchors[key]) is not None
    def is_home(self,key):return self.panel_location(key)=='home'
    def is_visible(self,key):
        group=self._floating_group(key)
        if group is not None:
            host=self._float_hosts.get(group['id'])
            return bool(host and host.window.winfo_viewable() and group['selected']==key)
        return key not in self._hidden and bool(self.panels[key].widget.winfo_ismapped())

    def focus(self,key):
        if key in self._hidden:self.show(key)
        anchor=self.focus_home(key)
        if anchor is not None and callable(self.on_focus):self.on_focus(anchor)
        panel=self.panels[key]
        group=self._floating_group(key)
        if group is not None:
            host=self._float_hosts[group['id']];host.select(key)
            if self.workspace.winfo_viewable():host.window.deiconify();host.window.lift()
            else:host.window.withdraw()
        elif key in self._floating:
            if self.workspace.winfo_viewable():
                self.workspace.tk.call('wm','deiconify',panel.widget);panel.widget.lift()
            else:self.workspace.tk.call('wm','withdraw',panel.widget)
        else:
            for notebook in tuple(self._bound.values()):
                if notebook.winfo_exists() and str(panel.widget) in notebook.tabs():notebook.select(panel.widget);break
            if panel.widget.winfo_manager() not in ('notebook','wm'):panel.widget.lift()
        return panel.widget

    def show(self,key):
        previous=self._hidden.pop(key,None)
        if previous is not None:
            if previous.get('group'):
                identity=previous['group']
                group=self._floating_groups.get(identity)
                if group is None:
                    group=self._group([]);group['id']=identity;group['geometry']=previous['geometry']
                    self._floating_groups[identity]=group
                group['tabs'].append(key);group['selected']=key
            elif previous.get('floating'):
                self._floating[key]=previous['floating']
            else:
                anchor=previous.get('anchor') or self.focus_home(key)
                target=previous.get('neighbor')
                _,group=self._find(target) if target else (None,None)
                if group is not None:group['tabs'].append(key);group['selected']=key
                elif self._roots.get(anchor) is None:self._roots[anchor]=self._group([key])
                else:
                    group=next(node for node in self._nodes(self._roots[anchor]) if node['type']=='group')
                    group['tabs'].append(key);group['selected']=key
            if self._active:self._render()
            else:
                panel=self.panels[key];self._mount(panel.widget,panel.home);panel.location='home'
                self._sync_grid_homes()
            self._changed()
        return self.focus(key)

    def hide(self,key):
        if key in self._hidden:return
        self._capture_ratios();anchor,group=self._find(key)
        previous={'anchor':anchor,'neighbor':next((item for item in group['tabs'] if item!=key),None) if group else None,
                  'floating':self._float_geometry(key) if key in self._floating else None}
        floated=self._floating_group(key)
        if floated is not None:previous.update(group=floated['id'],geometry=floated['geometry'])
        self._remove(key);self._hidden[key]=previous
        if floated is None:self._detach(self.panels[key].widget)
        if self._active:self._render()
        else:self.panels[key].location='hidden';self._sync_grid_homes()
        self._changed()

    def _geometry(self,key,geometry=None):
        panel=self.panels[key];w,h=panel.minsize
        if geometry and re.fullmatch(r'\d+x\d+[+-]\d+[+-]\d+',str(geometry)):
            return str(geometry)
        width=max(self._px(w),panel.widget.winfo_width(),self._px(360))
        height=max(self._px(h),panel.widget.winfo_height(),self._px(240))
        x=max(0,self.workspace.winfo_rootx()+self._px(40));y=max(0,self.workspace.winfo_rooty()+self._px(40))
        return f'{width}x{height}+{x}+{y}'

    def _screen_bounds(self):
        if os.name=='nt':
            import ctypes
            user=ctypes.WinDLL('user32')
            left,top,width,height=(user.GetSystemMetrics(index) for index in (76,77,78,79))
            if width>0 and height>0:return left,top,width,height
        return 0,0,self.workspace.winfo_screenwidth(),self.workspace.winfo_screenheight()

    def _restore_geometry(self,key,geometry,saved_scale):
        geometry=self._geometry(key,geometry)
        match=re.fullmatch(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)',geometry)
        width,height,x,y=map(int,match.groups())
        factor=self.scale/saved_scale if saved_scale>0 else 1.
        left,top,sw,sh=self._screen_bounds()
        minimum=self.panels[key].minsize
        width=min(sw,max(self._px(minimum[0]),round(width*factor)))
        height=min(sh,max(self._px(minimum[1]),round(height*factor)))
        # Keep a title/drag area reachable after monitors are unplugged. Tk's
        # negative geometry coordinates are right/bottom offsets, so use the
        # primary screen for an entirely missing or negative saved location.
        if x<0 or x+width<left+self._px(40) or x>left+sw-self._px(40):x=max(0,left)+self._px(40)
        if y<0 or y+height<top+self._px(40) or y>top+sh-self._px(40):y=max(0,top)+self._px(40)
        return f'{width}x{height}{x:+d}{y:+d}'

    def _add_grip(self,panel):
        if panel.header is not None or panel.grip is not None:return
        widget=panel.widget
        grip=tk.Frame(widget,bg=PANEL,height=self._px(28),cursor='fleur')
        tk.Label(grip,text=panel.title+'   ⋮⋮',bg=PANEL,fg=TEXT,anchor='w').pack(side='left',padx=self._px(8))
        ttk.Button(grip,text='停靠',style='Dock.TButton',command=lambda:self._return_home(panel.key)).pack(side='right')
        packed=widget.pack_slaves();gridded=widget.grid_slaves()
        if gridded:
            count=widget.grid_size()[1]
            rows=[dict(widget.grid_rowconfigure(row)) for row in range(count)]
            children=[(child,int(child.grid_info()['row'])) for child in gridded]
            for row in range(count,0,-1):widget.grid_rowconfigure(row,**rows[row-1])
            widget.grid_rowconfigure(0,weight=0,minsize=self._px(28),pad=0,uniform='')
            for child,row in children:child.grid_configure(row=row+1)
            grip.grid(row=0,column=0,columnspan=max(1,widget.grid_size()[0]),sticky='ew')
            panel.grid_shift=(rows,children)
        else:
            options={'before':packed[0]} if packed else {}
            grip.pack(side='top',fill='x',**options)
        panel.grip=grip;self._bind_header(grip,panel.key)

    def _remove_grip(self,panel):
        if panel.grip is None:return
        panel.grip.destroy();panel.grip=None
        if panel.grid_shift:
            rows,children=panel.grid_shift
            for child,row in children:
                if child.winfo_exists():child.grid_configure(row=row)
            for row,options in enumerate(rows):panel.widget.grid_rowconfigure(row,**options)
            panel.widget.grid_rowconfigure(len(rows),weight=0,minsize=0,pad=0,uniform='')
            panel.grid_shift=None

    def _manage_float(self,key,geometry):
        panel=self.panels[key];widget=panel.widget
        if widget.winfo_manager()!='wm':
            self._detach(widget);self._add_grip(panel)
            self.workspace.tk.call('wm','manage',widget)
            self.workspace.tk.call('wm','title',widget,panel.title+' · OKK STUDIO')
            self.workspace.tk.call('wm','minsize',widget,*map(self._px,panel.minsize))
            if panel.close_command is None:panel.close_command=widget.register(lambda:self._return_home(key))
            self.workspace.tk.call('wm','protocol',widget,'WM_DELETE_WINDOW',panel.close_command)
            self.workspace.tk.call('wm','geometry',widget,geometry)
            if not self.workspace.winfo_viewable():self.workspace.tk.call('wm','withdraw',widget)
        panel.location='floating'

    def _float_geometry(self,key):
        try:return str(self.workspace.tk.call('wm','geometry',self.panels[key].widget))
        except tk.TclError:return self._floating.get(key)

    def float_panel(self,key,geometry=None):
        if key not in self.panels:raise KeyError(key)
        self._capture_ratios();geometry=self._geometry(key,geometry)
        self._remove(key);self._floating[key]=geometry
        self._render();self.focus(key);self._changed()

    def float_group(self,key,geometry=None):
        if key not in self.panels:raise KeyError(key)
        self._capture_ratios()
        existing=self._floating_group(key)
        if existing is not None:
            if geometry:
                existing['geometry']=self._geometry(key,geometry)
                self._float_hosts[existing['id']].window.geometry(existing['geometry'])
            self.focus(key);self._changed();return
        keys=self.group_members(key)
        _,old=self._find(key);selected=old.get('selected',key) if old else key
        geometry=self._geometry(key,geometry or self._floating.get(key))
        before=self.serialize()
        for item in keys:self._remove(item)
        group=self._group(keys);group.update(selected=selected,geometry=geometry)
        self._floating_groups[group['id']]=group
        try:self._render()
        except Exception:
            # Native attach owns rollback; never leave a half-built empty shell
            # after a supported, recoverable attachment failure.
            self.restore(before)
            raise
        self.focus(selected);self._changed()

    def return_group_home(self,key):
        keys=self.group_members(key);anchor=self._home_anchors[key]
        self._capture_ratios()
        for item in keys:self._remove(item)
        root=self._roots.get(anchor)
        if root is None:self._roots[anchor]=self._group(keys)
        else:
            group=next(node for node in self._nodes(root) if node['type']=='group')
            group['tabs'].extend(keys);group['selected']=key
        self._render();self.focus(key);self._changed()

    def dock_group(self,key,target,side='center'):
        return self._dock_members(self.group_members(key),target,side)

    def _return_home(self,key):
        anchor=self._home_anchors[key]
        self._capture_ratios();self._remove(key)
        root=self._roots.get(anchor)
        if root is None:self._roots[anchor]=self._group([key])
        else:
            group=next(node for node in self._nodes(root) if node['type']=='group')
            group['tabs'].append(key);group['selected']=key
        self._render();self.focus(key);self._changed()

    def dock(self,key,target,side='center'):
        return self._dock_members((key,),target,side)

    def _dock_members(self,keys,target,side='center'):
        if side not in ('center','left','right','top','bottom'):raise ValueError('Invalid docking side')
        for key in (*keys,target):
            if key not in self.panels:raise KeyError(key)
        if target in keys:return self.focus(target)
        if target in self._hidden:self.show(target)
        self._capture_ratios()
        for key in keys:self._remove(key)
        group=self._floating_group(target)
        if target in self._floating:
            geometry=self._floating.pop(target);group=self._group([target]);group['geometry']=geometry
            self._floating_groups[group['id']]=group
        if group is not None:
            group['tabs'].extend(keys);group['selected']=keys[0]
        else:
            anchor,group=self._find(target)
            if group is None:raise ValueError('目标面板没有停靠位置。')
            if side=='center':group['tabs'].extend(keys);group['selected']=keys[0]
            else:
                new=self._group(keys);before=side in ('left','top')
                split={'type':'split','orient':'horizontal' if side in ('left','right') else 'vertical',
                       'ratio':.5,'children':[new,group] if before else [group,new]}
                self._replace_node(anchor,group,split)
        self._render();self.focus(keys[0]);self._changed()

    def restore_defaults(self):
        self._cancel_drag()
        self._close_float_hosts();self._floating_groups={}
        for panel in self.panels.values():self._detach(panel.widget);self._remove_grip(panel)
        self._clear_runtime()
        for entry in self._anchors.values():
            if entry['original'] not in [panel.widget for panel in self.panels.values()]:
                self._detach(entry['original']);self._mount(entry['original'],entry['home'])
        for anchor,header in self._home_headers.items():
            header.pack(side='top',fill='x',before=self._anchors[anchor]['original'])
        for panel in sorted(self.panels.values(),key=lambda item:item.home.index):
            if panel.key in self._default_hidden:panel.location='hidden'
            else:self._mount(panel.widget,panel.home);panel.location='home'
        self._roots=copy.deepcopy(self._default_roots);self._floating={};self._hidden=copy.deepcopy(self._default_hidden);self._active=False
        self._sync_grid_homes()
        self._raise_panels()
        self._changed()

    def _sync_grid_homes(self):
        # An emptied monitor column must not retain half the workspace through
        # its former uniform/weight settings. Other grid occupants win.
        for (container,axis,index),options in self._grid_defaults.items():
            occupied=container.grid_slaves(**{axis:index})
            settings=options if occupied else {'weight':0,'minsize':0,'pad':0,'uniform':''}
            getattr(container,'grid_'+axis+'configure')(index,**settings)

    def serialize(self):
        self._capture_ratios()
        return {'version':self.VERSION,'active':self._active,'roots':copy.deepcopy(self._roots),
                'floating':{key:self._float_geometry(key) for key in self._floating},
                'floating_groups':copy.deepcopy(list(self._floating_groups.values())),
                'hidden':copy.deepcopy(self._hidden),'scale':self.scale}

    def restore(self,layout):
        if not isinstance(layout,dict) or layout.get('version') not in (1,self.VERSION):raise ValueError('不支持的面板布局版本。')
        seen=set()
        def validate(node,depth=0):
            if node is None:return None
            if not isinstance(node,dict) or depth>24:raise ValueError('面板布局层级无效。')
            if node.get('type')=='group':
                tabs=node.get('tabs')
                if not isinstance(tabs,list) or not tabs:raise ValueError('空标签组。')
                for key in tabs:
                    if key not in self.panels or key in seen:raise ValueError('未知或重复面板。')
                    seen.add(key)
                group=self._group(tabs);group['selected']=node.get('selected') if node.get('selected') in tabs else tabs[0]
                return group
            if node.get('type')!='split' or node.get('orient') not in ('horizontal','vertical') or len(node.get('children',[]))!=2:
                raise ValueError('面板分割布局无效。')
            ratio=float(node.get('ratio',.5))
            if not math.isfinite(ratio):raise ValueError('面板分割比例无效。')
            children=[validate(child,depth+1) for child in node['children']]
            if any(child is None for child in children):raise ValueError('空分割节点。')
            return {'type':'split','orient':node['orient'],'ratio':max(.05,min(.95,ratio)),'children':children}
        roots=layout.get('roots',{})
        if not isinstance(roots,dict) or set(roots)!=set(self._anchors):raise ValueError('布局与当前工作区不匹配。')
        roots={anchor:validate(node) for anchor,node in roots.items()}
        floating=dict(layout.get('floating',{}));hidden=copy.deepcopy(dict(layout.get('hidden',{})))
        floating_groups={};restored_group_ids={}
        for item in layout.get('floating_groups',[]):
            group=validate(item)
            if group is None or group['type']!='group':raise ValueError('浮动标签组无效。')
            saved_identity=item.get('id')
            if saved_identity:
                if not isinstance(saved_identity,str) or saved_identity in restored_group_ids:
                    raise ValueError('浮动标签组标识无效或重复。')
                restored_group_ids[saved_identity]=group['id']
            group['geometry']=item.get('geometry')
            floating_groups[group['id']]=group
        for previous in hidden.values():
            if not isinstance(previous,dict):raise ValueError('隐藏面板位置无效。')
            saved_identity=previous.get('group')
            if saved_identity:
                if not isinstance(saved_identity,str):raise ValueError('隐藏面板组标识无效。')
                # All group IDs are regenerated when restoring. Hidden tabs
                # must follow the same mapping, including a group with every
                # tab hidden (which has no live floating_groups entry).
                if saved_identity not in restored_group_ids:
                    restored_group_ids[saved_identity]=self._group([])['id']
                previous['group']=restored_group_ids[saved_identity]
        for key in [*floating,*hidden]:
            if key not in self.panels or key in seen:raise ValueError('未知或重复面板。')
            seen.add(key)
        if seen!=set(self.panels):raise ValueError('布局缺少面板。')
        if not layout.get('active',True):
            self.restore_defaults()
            for key in tuple(self._hidden):
                if key not in hidden:self.show(key)
            for key in hidden:
                if key not in self._hidden:self.hide(key)
            return
        saved_scale=float(layout.get('scale',self.scale))
        if not math.isfinite(saved_scale) or saved_scale<=0:raise ValueError('布局缩放比例无效。')
        for key in floating:floating[key]=self._restore_geometry(key,floating[key],saved_scale)
        for group in floating_groups.values():
            group['geometry']=self._restore_geometry(group['tabs'][0],group['geometry'],saved_scale)
        self._close_float_hosts()
        for panel in self.panels.values():self._detach(panel.widget);self._remove_grip(panel)
        self._roots=roots;self._floating=floating;self._hidden=hidden;self._floating_groups=floating_groups
        self._render();self._changed()

    def save(self,path):
        path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.pending')
        try:
            temporary.write_text(json.dumps(self.serialize(),ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temporary,path)
        finally:
            if temporary.exists():temporary.unlink()

    def load(self,path):self.restore(json.loads(Path(path).read_text(encoding='utf-8')))

    def _targets(self):
        for group in self._floating_groups.values():
            if self._drag and self._drag.get('whole_group') and self._drag['key'] in group['tabs']:continue
            host=self._float_hosts.get(group['id'])
            if host and host.window.winfo_viewable():yield group['tabs'][0],host.notebook
        for key in self._floating:
            if self.panels[key].widget.winfo_viewable():yield key,self.panels[key].widget
        if self._active:
            for node,notebook in self._groups.values():
                if node['tabs'] and notebook.winfo_viewable():yield node['tabs'][0],notebook
        else:
            for anchor,node in self._roots.items():
                widget=self._anchors[anchor]['original']
                if node and widget.winfo_viewable():yield node['tabs'][0],widget

    def drop_target(self,x,y):
        for key,widget in self._targets():
            left,top=widget.winfo_rootx(),widget.winfo_rooty();width,height=widget.winfo_width(),widget.winfo_height()
            if left<=x<left+width and top<=y<top+height:
                distances={'left':(x-left)/width,'right':(left+width-x)/width,
                           'top':(y-top)/height,'bottom':(top+height-y)/height}
                side=min(distances,key=distances.get)
                if distances[side]>.23 or key in self._floating_keys():side='center'
                return key,side,(left,top,width,height)
        return None

    def _press(self,event,key,*,whole_group=False):
        if self._closed:return
        self._drag={'key':key,'x':event.x_root,'y':event.y_root,'widget':event.widget,'started':False,'whole_group':whole_group}

    def _motion(self,event):
        drag=self._drag
        if drag is None:return
        if not drag['started']:
            if abs(event.x_root-drag['x'])+abs(event.y_root-drag['y'])<self._px(8):return
            drag['started']=True
            try:drag['widget'].grab_set()
            except tk.TclError:pass
        target=self._drag_target(event.x_root,event.y_root,drag['key'])
        if target and target[0]==drag['key']:
            _,group=self._find(drag['key'])
            if group is None or len(group['tabs'])==1:target=None
        self._show_hint(target)
        return 'break'

    def _drag_target(self,x,y,key):
        target=self.drop_target(x,y)
        if target and self._drag and self._drag.get('whole_group') and target[0] in self.group_members(key):return None
        if target and target[0]==key:
            neighbor=next((item for item in self.group_members(key) if item!=key),None)
            if neighbor:target=(neighbor,target[1],target[2])
        return target

    def _show_hint(self,target):
        if target is None:
            if self._overlay:self._overlay.withdraw()
            return
        key,side,(x,y,width,height)=target
        if side in ('left','right'):
            width//=2
            if side=='right':x+=width
        elif side in ('top','bottom'):
            height//=2
            if side=='bottom':y+=height
        if self._overlay is None:
            overlay=tk.Toplevel(self.workspace);overlay.withdraw();overlay.overrideredirect(True)
            overlay.configure(bg=ACCENT);overlay.attributes('-topmost',True)
            try:overlay.attributes('-alpha',.38)
            except tk.TclError:pass
            label=tk.Label(overlay,bg=ACCENT,fg='white',font=('Microsoft YaHei UI',12,'bold'))
            label.pack(expand=True);overlay._dock_label=label;self._overlay=overlay
        self._overlay._dock_label.configure(text={'center':'合并为标签','left':'停靠到左侧','right':'停靠到右侧','top':'停靠到上方','bottom':'停靠到下方'}[side])
        self._overlay.geometry(f'{max(1,width)}x{max(1,height)}{x:+d}{y:+d}')
        self._overlay.deiconify()

    def _release(self,event):
        drag=self._drag
        if drag is None:return
        started=drag['started'];key=drag['key'];target=self._drag_target(event.x_root,event.y_root,key) if started else None
        self._cancel_drag()
        if not started:return
        if target and target[0]!=key:
            (self.dock_group if drag.get('whole_group') else self.dock)(key,target[0],target[1])
        elif not target:
            panel=self.panels[key];w=max(self._px(panel.minsize[0]),panel.widget.winfo_width())
            h=max(self._px(panel.minsize[1]),panel.widget.winfo_height())
            (self.float_group if drag.get('whole_group') else self.float_panel)(key,f'{w}x{h}+{max(0,event.x_root-self._px(30))}+{max(0,event.y_root-self._px(12))}')
        return 'break'

    def _cancel_drag(self,event=None):
        if self._drag:
            try:self._drag['widget'].grab_release()
            except tk.TclError:pass
        self._drag=None
        if self._overlay:
            self._overlay.destroy();self._overlay=None
        return 'break'

    def _workspace_map(self,event):
        if event.widget is not self.workspace or self._closed:return
        visible=event.type==tk.EventType.Map
        for host in self._float_hosts.values():
            try:host.window.deiconify() if visible else host.window.withdraw()
            except tk.TclError:pass
        for key in self._floating:
            try:self.workspace.tk.call('wm','deiconify' if visible else 'withdraw',self.panels[key].widget)
            except tk.TclError:pass

    def close(self):
        if self._closed:return
        self._cancel_drag();self._close_float_hosts();self._closed=True
        for panel in self.panels.values():
            try:self._detach(panel.widget);self._remove_grip(panel)
            except tk.TclError:pass
        self._clear_runtime()
        for widget,event,identity in self._binding_ids:
            try:
                if widget.winfo_exists():widget.unbind(event,identity)
            except tk.TclError:pass
        self._binding_ids=[]

