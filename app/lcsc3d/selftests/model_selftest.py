"""Offline frozen-app exercise of real STEP placement, GUI and resource output.

The explicitly selected folder supplies fixtures/C2765186.json, source.step
and source.obj. OBJ supplies only the source placement anchor.
"""
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import time
import zlib

import olefile
import zstandard
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication


def start(window, folder):
    main = sys.modules[type(window).__module__]
    root = Path(folder).resolve()
    root.mkdir(parents=True,exist_ok=True)
    source = json.loads((root/'fixtures/C2765186.json').read_text('utf-8'))
    step = (root/'fixtures/source.step').read_bytes()
    obj = (root/'fixtures/source.obj').read_text('utf-8')
    data = {p:copy.deepcopy(source) for p in ('C2765186','C900001','C900002','C900003','C900004')}
    for part in data:
        doc = data[part]['packageDetail']['dataStr']
        if isinstance(doc,str):
            doc = data[part]['packageDetail']['dataStr'] = json.loads(doc)
        if part == 'C900003':
            doc['shape'] = [s for s in doc['shape'] if not s.startswith('SVGNODE~')]
            doc['head'].pop('uuid_3d',None)
            continue
        for i,s in enumerate(doc['shape']):
            if s.startswith('SVGNODE~'):
                node = json.loads(s.partition('~')[2])
                if part == 'C900001':
                    node['attrs']['c_rotation'] = '20,30,40'
                elif part == 'C900002':
                    node['attrs']['c_origin'] = ''
                elif part == 'C900004':
                    for key in ('c_width','c_height'):
                        node['attrs'][key] = str(float(node['attrs'][key])*2)
                doc['shape'][i] = 'SVGNODE~'+json.dumps(node)

    class OfflineApi(main.NetworkApi):
        def fetch(self,*args,**kwargs):
            raise AssertionError('Offline model self-test attempted network access')
        def get_cad_data_of_component(self,part):
            self.check_cancelled()
            return copy.deepcopy(data[part])
        def get_step_3d_model(self,identifier):
            self.check_cancelled()
            return step
        def get_raw_3d_model_obj(self,identifier):
            self.check_cancelled()
            return obj

    main.NetworkApi = OfflineApi
    window.request_component_info = lambda:None
    window.show_preview = lambda *args,**kwargs:None
    window.resize(1380,880)
    cases = [('ad','ad',False,False,'embedded',['C2765186']),
             ('ad-merge','ad',True,False,'embedded',['C2765186','C900001']),
             ('ad-copies','ad',True,True,'embedded',['C2765186','C900001']),
             ('ad-original-size','ad',False,False,'embedded',['C900004']),
             ('kicad-embedded','kicad',False,False,'embedded',['C2765186']),
             ('kicad-external','kicad',False,False,'external',['C2765186']),
             ('kicad-copies','kicad',True,True,'embedded',['C2765186','C900001']),
             ('kicad-external-copies','kicad',True,True,'external',['C2765186','C900001']),
             ('mixed','kicad',True,False,'embedded',['C2765186','C900002','C900003'])]
    state = {'index':-1,'phase':'next','deadline':time.monotonic()+300,'done':False}
    report = {'version':main.VERSION,'frozen':bool(getattr(sys,'frozen',False)),'cases':[]}
    timer = QTimer(window)
    timer.setInterval(100)

    def finish(error=''):
        state['done'] = True
        timer.stop()
        report.update(success=not error,error=error)
        (root/'model-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if window.worker and window.worker.isRunning():
            window.worker.cancelled.set()
        def close():
            if window.worker and window.worker.isRunning():
                QTimer.singleShot(100,close)
            else:
                window.close()
                QApplication.instance().exit(1 if error else 0)
        close()

    def tick():
        try:
            if time.monotonic()>state['deadline']:
                raise AssertionError('3D GUI verification timed out')
            if state['phase']=='next':
                state['index']+=1
                if state['index']==len(cases):
                    assert getattr(window,'_model_toast',None) is not None
                    window.grab().save(str(root/'model-notice.png'))
                    finish()
                    return
                name,target,merge,keep,mode,parts = cases[state['index']]
                state.update(name=name,target=target,parts=parts,mode=mode,output=root/name,phase='wait')
                assert window.bind_3d_box.isChecked()
                window.step_box.setChecked(False)
                window.obj_box.setChecked(False)
                window.lib_append_box.setChecked(False)
                window.export_target_box.setCurrentIndex(window.export_target_box.findData(target))
                window.schlib_box.setChecked(True)
                window.pcblib_box.setChecked(True)
                window.lib_merge_box.setChecked(merge)
                window.merge_schlib_box.setChecked(merge)
                window.merge_pcblib_box.setChecked(merge)
                window.keep_schlib_box.setChecked(keep)
                window.keep_pcblib_box.setChecked(keep)
                window.kicad_symbol_box.setChecked(True)
                window.kicad_footprint_box.setChecked(True)
                window.kicad_lib_merge_box.setChecked(merge)
                window.kicad_merge_symbol_box.setChecked(merge)
                window.kicad_merge_footprint_box.setChecked(merge)
                window.keep_kicad_symbol_box.setChecked(keep)
                window.keep_kicad_footprint_box.setChecked(keep)
                window.kicad_3d_mode_box.setCurrentIndex(window.kicad_3d_mode_box.findData(mode))
                window.path_input.setText(str(state['output']))
                window.input.setPlainText('\n'.join(parts))
                assert window.load_queue()
                window.start_batch()
            elif state['phase']=='wait' and not window.batch_running and not (window.worker and window.worker.isRunning()):
                rows = [window.results[p] for p in state['parts']]
                assert [r.status for r in rows] == (['成功','部分完成','成功'] if state['name']=='mixed' else ['成功']*len(rows)), [r.message for r in rows]
                paths = sorted({Path(p) for r in rows for p in r.files})
                assert all(p.is_file() for p in paths)
                model_count = 0
                for path in paths:
                    if path.suffix.lower()=='.pcblib':
                        with olefile.OleFileIO(path) as ole:
                            count = struct.unpack('<I',ole.openstream('Library/Models/Header').read())[0]
                            assert count>0
                            for i in range(count):
                                payload = zlib.decompress(ole.openstream('Library/Models/'+str(i)).read())
                                assert payload==step
                                model_count+=1
                            # Independent golden pose for the real C2765186:
                            # source anchor (0, .125, -2.58), JSON (0,-1.1999976,-.8999982).
                            if state['name'] in ('ad','ad-original-size'):
                                from lcsc3d.eda.library_merge import Reader, parameters
                                poses = []
                                for stream in ole.listdir():
                                    if len(stream)!=2 or stream[0]=='Library' or stream[1]!='Data':
                                        continue
                                    reader = Reader(ole.openstream(stream).read())
                                    reader.string()
                                    while reader.offset < len(reader.data):
                                        kind = reader.take(1)[0]
                                        blocks = [reader.block() for _ in range(6 if kind==2 else 2 if kind==5 else 1)]
                                        if kind==12:
                                            values = parameters(Reader(blocks[0][1][18:]).block()[1])
                                            poses.append([float(values[k].removesuffix('mil'))*.0254
                                                for k in ('MODEL.2D.X','MODEL.2D.Y','MODEL.3D.DZ')])
                                assert len(poses)==1,poses
                                assert max(abs(a-b) for a,b in zip(poses[0],(0,-1.3249976,1.6800018)))<.000003,poses
                    elif path.suffix=='.kicad_mod':
                        text = path.read_text('utf-8')
                        for payload in re.findall(r'\(data \|([^|]+)\|\)',text):
                            decoded = zstandard.ZstdDecompressor().decompress(base64.b64decode(payload))
                            assert decoded==step
                            assert hashlib.sha256(decoded).hexdigest() in text
                            model_count+=1
                        if state['mode']=='external':
                            for name in re.findall(r'\(model "([^"]+)"',text):
                                assert (path.parent/name).read_bytes()==step
                                model_count+=1
                assert model_count>0
                report['cases'].append({'name':state['name'],'statuses':[r.status for r in rows],
                    'model_instances':model_count,'files':[str(p) for p in paths],
                    'diagnostics':{r.part:r.model_diagnostics for r in rows},'success':True})
                window.grab().save(str(root/(state['name']+'.png')))
                state['phase']='next'
        except Exception as exc:
            finish(type(exc).__name__+': '+str(exc))
    timer.timeout.connect(tick)
    timer.start()
