"""Verify a staged release and package source after updating its validation record."""
import argparse,hashlib,json,re,shutil,subprocess,sys,types,zipfile
from datetime import date
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader
root=Path(__file__).resolve().parent.parent
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--test-count',type=int,required=True)
parser.add_argument('--output-dir',type=Path,default=root/'outputs')
parser.add_argument('--portable-dir',type=Path,default=root/'work/便携验证/验证结果')
parser.add_argument('--update-report',type=Path,default=root/'work/self-update-verification.json')
parser.add_argument('--store-dir',type=Path)
parser.add_argument('--store-offline-dir',type=Path)
parser.add_argument('--settings-dir',type=Path)
parser.add_argument('--merge-dir',type=Path)
parser.add_argument('--integration-dir',type=Path)
parser.add_argument('--export-dir',type=Path)
parser.add_argument('--capture-dir',type=Path)
parser.add_argument('--local-update-report',type=Path)
parser.add_argument('--startup-silent-reports',nargs='+',type=Path)
parser.add_argument('--verification-date',default=date.today().isoformat())
args=parser.parse_args()
assert args.test_count>0
outputs,tested=args.output_dir.resolve(),args.portable_dir.resolve()
outputs.mkdir(parents=True,exist_ok=True)
version=re.search(r"VERSION = '([^']+)'",(root/'app/main.py').read_text(encoding='utf-8')).group(1)
report=json.loads((tested/'verification.json').read_text(encoding='utf-8'))
update=json.loads(args.update_report.read_text(encoding='utf-8'))
assert report['version']==update['version']==version
assert report['frozen'] and report['preview']=='ready' and report['second_3d_ready']
assert report['application_name']==report['window_title']=='LCSC3D'
assert report['automatic_preview'] and report['viewer_page_loads']==1
assert report['preview_window']=={'hwnd_preserved':True,'events':[]}
assert not report['csv_files'] and not report['non_model_exports']
assert [r['status'] for r in report['results']]==['成功','成功','失败']
assert report['download_selection']=={'checked_ids':['C2040','C20197','C999999999999'],'unchecked_ids':['C163691'],'selected_only':True}
for result in report['results'][:2]:
 assert {Path(name).suffix for name in result['files']}=={'.step','.obj'}
 assert set(Path(result['folder']).iterdir())=={Path(name) for name in result['files']}
 for name in result['files']:
  path=Path(name)
  assert path.is_file() and path.stat().st_size>0
  if path.suffix=='.step':assert b'ISO-10303-21' in path.read_bytes()[:2048]
 count=57 if result['part']=='C2040' else 8
 preview=report['library_previews'][result['part']]
 assert preview['symbol_pins']==[count] and preview['footprint_pads']==count and not preview['errors']
for key in ('frozen_helper','original_exited','replacement_verified','startup_acknowledged','settings_preserved'):assert update[key]
store_text=''
if args.store_dir:
 offline=json.loads((args.store_dir/'原生商城离线/favorites-verification.json').read_text(encoding='utf-8'))
 live=json.loads((args.store_dir/'实号恢复与分页搜索/store-live-verification.json').read_text(encoding='utf-8'))
 assert all(r['success'] and r['frozen'] and r['version']==version for r in (offline,live))
 for key in ('independent_price_tiers','price_precision','stock_column','price_tier_survives_paging','full_description','wrapped_parameter_values','queue_delete_checked','queue_delete_preserves_files','native_password_login','native_sms_login','native_image_captcha'):assert offline[key]
 assert live['restored_without_qr'] and live['real_price_tier_switch']
 store_text=('- 原生商城离线与实网 EXE 验证通过：50 条分页、跨页选择、价格梯度、最低起订量、库存、完整文字、商品原图、账号收藏、三种登录与保存会话恢复，以及删除勾选器件后保留文件。\n'
             '- 登录写入及图片验证使用本地模拟服务回归；短信和图片验证码在开发阶段由用户完成真实验证。本次真实账号验证仅恢复已保存会话并读取收藏，公开截图使用未登录的公开商品资料。\n')
if args.store_offline_dir:
 offline=json.loads((args.store_offline_dir/'favorites-verification.json').read_text(encoding='utf-8'))
 assert offline['success'] and offline['frozen'] and offline['version']==version
 for key in ('qr_native','native_password_login','native_sms_login','native_image_captcha','restore_without_qr','logout_clears_account','independent_price_tiers','queue_delete_preserves_files'):assert offline[key]
 store_text+=('- 原生商城离线 EXE 验证通过：扫码、密码、短信与图片验证、加密保存和恢复、退出清除、分页、收藏及商品资料。登录和短信仅使用受控本地服务，不访问用户真实账号。\n')
settings_text=''
merge_text=''
integration_text=''
if args.export_dir:
 export=json.loads((args.export_dir/'export-verification.json').read_text(encoding='utf-8'))
 assert export['success'] and export['frozen'] and export['version']==version
 expected={'merge-grouped','merge-individual','append-grouped','append-individual',
           'append-symbol','append-footprint','project-grouped','project-individual','empty-import'}
 assert {case['name'] for case in export['cases']}==expected
 assert all(case['success'] for case in export['cases'])
 sys.path.insert(0,str(root/'app/tests'))
 from altium_inspect import schematic,pcb,merged_pcb_section
 for case in export['cases']:
  for row in case.get('results',[]):
   assert row['status']=='成功'
   files=[Path(name) for name in row['files']]
   assert all(path.is_file() for path in files)
   for path in files:
    single=path.parent.name.endswith('_'+row['part'])
    expected_count={'C2040':57,'C20197':8}[row['part']]
    if path.suffix=='.SchLib':
     header,records,pins=schematic(path.read_bytes(),None if single else row['part']+'_Symbol')
     assert len(pins)==expected_count
     counterpart=next((other for other in files if other.suffix=='.PcbLib' and
                       other.parent.name.endswith('_'+row['part'])==single),None)
     if counterpart:
      board=counterpart.read_bytes()
      name,_,_=pcb(board,None if single else merged_pcb_section(board,row['part']))
      assert next(record['MODELNAME'] for record in records if record.get('RECORD')=='45')==name
    elif path.suffix=='.PcbLib':
     board=path.read_bytes()
     name,pads,primitives=pcb(board,None if single else merged_pcb_section(board,row['part']))
     assert len(pads)==expected_count
 integration_text+='- Lib 模式专项通过：136 组合覆盖合并/追加、单库/双库、STEP/OBJ、独立导出、工程联动开关及项目重复追加；另测空列表导入、模式恢复、不同库目录、取消、无效库、写入失败与文件竞争。\n'
 integration_text+='- 冻结 EXE 离线执行 9 个完整窗口流程，确认互斥/按需显示、追加路径禁用、集中 3D 目录、配套独立库和空列表直接导入；生成的 AD 文件再由独立测试读取器验证。\n'
 integration_text+='- 新增导出规划、目录规则、库提交和空列表工程导入日志；隔离配置验证日志打包/清除后继续写入，不读取真实登录会话。\n'
 integration_text+='- 工程引用、原文件保留及库内引脚/焊盘/配套引用已自动核验；本次未在 Altium Designer 界面中重新加载工程或实际放置器件。\n'
if args.integration_dir:
 integration=json.loads((args.integration_dir/'verification.json').read_text(encoding='utf-8'))
 assert integration['success'] and integration['frozen'] and integration['integrated'] and integration['version']==version
 sys.path.insert(0,str(root/'app/tests'))
 from altium_inspect import schematic,pcb,merged_pcb_section
 import olefile
 for row in integration['results']:
  assert row['status']=='成功' and len(row['files'])==4
  singles=[Path(name) for name in row['files'] if Path(name).parent!=args.integration_dir.resolve()]
  sch=next(file for file in singles if file.suffix=='.SchLib')
  board=next(file for file in singles if file.suffix=='.PcbLib')
  _,records,_=schematic(sch.read_bytes())
  name,_,_=pcb(board.read_bytes())
  assert next(record['MODELNAME'] for record in records if record.get('RECORD')=='45')==name
 for key,ext in (('schlib_target','SchLib'),('pcblib_target','PcbLib')):
  with olefile.OleFileIO(str(args.integration_dir/('原始库.'+ext))) as old, olefile.OleFileIO(integration['export_targets'][key]) as new:
   assert new.root.clsid==old.root.clsid
   for path in old.listdir():
    if path not in (['FileHeader'],['SectionKeys'],['Library','Data']):
     assert old.openstream(path).read()==new.openstream(path).read(),path
 project=Path(integration['export_targets']['project_path']).read_text(encoding='utf-8')
 assert 'Custom=preserved' in project and 'DocumentPath=existing.PcbDoc' in project
 assert project.count('DocumentPath=')==3 and '已有库.SchLib' in project and '已有库.PcbLib' in project
 integration_text+='- 冻结 EXE 增量导出通过：追加到两份已有库并跳过同名项，同时生成逐器件配套库；原库条目与附加流逐字节保留。PCB 工程保留原文件引用及配置，新增两份合并库引用。\n'
 native_path=args.integration_dir/'native-verification.json'
 if native_path.is_file():
  native=json.loads(native_path.read_text(encoding='utf-8'))
  assert native['success'] and all(len(item['components'])==3 and not item['errors'] and not item['warnings'] for item in native['libraries'])
  integration_text+='- 追加后的 SchLib/PcbLib 由 AltiumSharp 1.0.2 独立读取并渲染全部条目，无错误或警告；AD 中重新加载工程与放置器件尚未实机验收。\n'
if args.capture_dir:
 capture=json.loads((args.capture_dir/'capture-verification.json').read_text(encoding='utf-8'))
 assert capture['success'] and capture['frozen'] and capture['version']==version
 assert capture['product_photo_preview'] and capture['store_import_notice'] and capture['public_products_only']
 integration_text+='- 冻结 EXE 实网界面验证通过：主页显示公开商品原图，商城加入下载列表后显示成功数量弹窗；不读取保存会话。\n'
if args.merge_dir:
 merge=json.loads((args.merge_dir/'verification.json').read_text(encoding='utf-8'))
 assert merge['success'] and merge['frozen'] and merge['version']==version and merge['merged']
 assert len(merge['parts'])>1
 sys.path.insert(0,str(root/'app/tests'))
 from altium_inspect import schematic,pcb,merged_pcb_section
 rows=merge['results']
 assert {row['part'] for row in rows}==set(merge['parts'])
 assert len({name for row in rows for name in row['files']})==2
 for row in rows:
  assert row['status']=='成功'
  sch=next(Path(name) for name in row['files'] if name.endswith('.SchLib'))
  board=next(Path(name) for name in row['files'] if name.endswith('.PcbLib'))
  assert sch.name=='项目符号.SchLib' and board.name=='项目封装.PcbLib'
  header,records,pins=schematic(sch.read_bytes(),row['part']+'_Symbol')
  payload=board.read_bytes()
  name,pads,_=pcb(payload,merged_pcb_section(payload,row['part']))
  assert int(header['COMPCOUNT'])==len(rows) and pins and pads
  assert next(record['MODELNAME'] for record in records if record.get('RECORD')=='45')==name
 merge_text=('- 冻结 EXE 合并验证通过：'+str(len(rows))+' 个公开器件经真实界面导出为自定义中文名称的两份库；独立读取器逐个核对条目、引脚/焊盘与符号到封装的引用。\n'
             '- 合并回归覆盖两个独立开关、重名及长名称、字体和几何保留、部分失败、取消保留原库、写入失败、仅替换本次条目及名称验证；合并库尚未经过 Altium Designer 实机打开验收。\n')
 native_path=args.merge_dir/'native-verification.json'
 if native_path.is_file():
  native=json.loads(native_path.read_text(encoding='utf-8'))
  assert native['success'] and {item['format'] for item in native['libraries']}=={'SchLib','PcbLib'}
  assert all(len(item['components'])==len(rows) and not item['errors'] and not item['warnings'] for item in native['libraries'])
  merge_text+='- 第二套独立读取器 AltiumSharp 1.0.2 成功读取并渲染两份合并库的全部条目，无警告或错误；未随应用打包。\n'
local_update_text=''
if args.local_update_report:
 local=json.loads(args.local_update_report.read_text(encoding='utf-8'))
 assert local['success'] and local['scenario']=='success'
 assert (local['ui']['check_clicked'] or local['ui'].get('startup_check') and local['ui'].get('notification_shown')) and local['ui']['download_clicked'] and local['ui']['sha256_verified']
 assert local['update']['status']=='success' and local['settings_preserved'] and local['exe_directory_clean']
 local_update_text='- 本地模拟更新源实际 EXE 验证通过：界面检查版本并点击下载按钮，经本机 HTTP 下载、SHA-256 校验、助手替换、重启确认并保留设置；不发布 GitHub，使用隔离副本。\n'
 if local['ui'].get('startup_check'):
  local_update_text+='- 启动后台检查真实 EXE 验证通过：发现新版自动展示可下载的更新窗口，复用已获取的版本信息。\n'
if args.startup_silent_reports:
 silent=[json.loads(path.read_text(encoding='utf-8')) for path in args.startup_silent_reports]
 assert {value['scenario'] for value in silent}=={'no-update','check-failure'}
 for value in silent:
  assert value['success'] and value['startup_check'] and value['ui']['silent']
  assert not value['ui']['notification_shown'] and not value['ui']['check_clicked'] and not value['ui']['download_clicked']
 local_update_text+='- 启动后台检查的“没有新版”和“检查失败”实际 EXE 验证均保持静默，未创建更新窗口，也未发起下载。\n'
if args.settings_dir:
 settings=json.loads((args.settings_dir/'settings-verification.json').read_text(encoding='utf-8'))
 assert settings['success'] and settings['frozen'] and settings['version']==version
 for key in ('default_system_proxies','default_debug','independent_proxy_preferences','saved_to_disk','restored_from_disk','log_level_filters_output','cancel_preserves_preferences','log_package_verified','log_clear_verified'):assert settings[key]
 settings_text=('- 冻结 EXE 设置验证通过：主窗口设置入口、商城与更新独立代理、Debug 默认等级、保存/恢复、取消保留和日志过滤。配置及日志使用隔离目录。\n'
                '- 本地 HTTP 服务回归核对实际代理/直连、忽略环境变量代理、已有客户端即时切换和 Cookie 保留；日志回归核对等级过滤、敏感数据排除与文件轮转。\n')
 if settings.get('app_data_settings') and settings.get('app_data_runtime'):
  settings_text+='- EXE 配置、日志和运行时解压验证位于 LOCALAPPDATA/LCSC3D，旧配置迁移与删除、默认导出和临时下载路径由本地回归核对。\n'
 settings_text+='- 冻结 EXE 日志工具验证通过：ZIP 内容与诊断字段、AppData 保存路径、清除前确认、清除后继续记录、运行状态标记及已打包 ZIP 保留。\n'
 if settings.get('library_options_restored'):
  settings_text+='- 冻结 EXE 保存与恢复“独立导出器件”、Lib 模式、已有库路径和 PCB 工程路径通过，使用隔离配置。\n'
for source,name in [('软件界面.png','软件界面'),('符号_C2040.png','符号预览'),('封装_C2040.png','封装预览'),('型号查询.png','型号查询')]:shutil.copyfile(tested/source,outputs/f'{name}.png')
shutil.copyfile(tested/'符号_C2040.png',root/'docs/images/app.png')
shutil.copyfile(tested/'封装_C2040.png',root/'docs/images/footprint.png')
text=f"""# LCSC3D {version} 成品验证

验证日期：{args.verification_date}。Windows x64、Python 3.12.10、PySide6 6.11.1。

- {args.test_count} 项本地回归通过，包含商城专项、下载列表删除、官方 STEP/OBJ、原生 AD 库、27 个官方 AD 样本、预览和自更新。
{settings_text}{store_text}{local_update_text}{merge_text}{integration_text}- 独立中文目录运行真实 EXE，清除 Python/Qt 环境变量，仅保留系统 PATH，退出码 0。
- C2040 与 C20197 各保存官方 STEP/OBJ；无效编号失败，未勾选 C163691 不下载，模型目录没有其他导出文件。
- 两次本地 3D 预览 ready，符号/封装分别识别 57/57 和 8/8 个引脚/焊盘，窗口句柄稳定。
- 冻结 EXE 自更新通过：原程序退出、独立进程替换、重启 Qt 窗口并确认、设置保留，耗时 {update['seconds']} 秒。使用隔离账号目录，未访问用户真实保存会话。
- 配置和更新暂存位于 AppData，EXE 目录清洁与跨盘更新：{update.get('exe_directory_clean', False)} / {update.get('cross_volume', False)}。
- 功能日志覆盖与故障定位：原因链、系统/服务错误码、文件/函数/行号、元件/格式/阶段、线程和更新助手关联经过故障注入回归；更新助手实际 EXE 日志关联：{update.get('diagnostic_correlation', False)}。
- GitHub 后台 runner 无可交互输入桌面时跳过实际鼠标前台切换；窗口归属仍检查，本地 Windows 桌面及成品验证覆盖实际前台句柄。
- README 截图由正式 EXE 读取公开元件并渲染，登录截图为空白表单，未访问用户保存会话或展示账号身份。
- EXE 内 Python 模块与当前源码逐个比较，源码 ZIP 全部文件与工作区一致，包含许可证和第三方声明。

成品：LCSC3D.exe。源码：LCSC3D.zip。校验和：SHA256SUMS.txt。
"""
(outputs/'验证记录.md').write_text(text,encoding='utf-8')
(root/'docs/验证记录.md').write_text(text,encoding='utf-8')
# Package after every source/documentation write so the archive matches this release.
subprocess.run([sys.executable,str(root/'scripts/package_source.py'),'--output-dir',str(outputs)],check=True)
archive_path=outputs/'LCSC3D.zip'
with zipfile.ZipFile(archive_path) as archive:
 assert archive.testzip() is None
 for name in archive.namelist():
  assert name.startswith('LCSC3D/') and not name.endswith('.csv')
  relative=name.removeprefix('LCSC3D/')
  assert not any(part in {'work','outputs','.venv','__pycache__','runtime'} for part in Path(relative).parts)
  assert not any(word in name for word in ('LCSC3D-settings.json','store-session.bin','.store-session-'))
  assert archive.read(name)==(root/relative).read_bytes(),name
def normalize(code):
 return code.replace(co_filename='',co_consts=tuple(normalize(v) if isinstance(v,types.CodeType) else v for v in code.co_consts))
executable_archive=CArchiveReader(str(outputs/'LCSC3D.exe'))
frozen=executable_archive.open_embedded_archive('PYZ.pyz')
for name in ('main','favorites','favorites_selftest','store','store_crypto','store_session','store_images','store_diagnostics','app_paths','app_settings','app_logging','log_support','settings_ui','settings_selftest','docs_capture','updater','update_ui','update_selftest','backend','altium','compound_storage','library_merge','altium_project','export_targets','export_selftest','product_preview','resources','model3d','library_preview'):
 code=compile((root/'app'/f'{name}.py').read_text(encoding='utf-8'),'','exec',dont_inherit=True)
 assert normalize(frozen.extract(name))==normalize(code),name
for name in ('viewer.html','vector_viewer.html'):
 assert executable_archive.extract(name)==(root/'app'/name).read_bytes(),name
hashes=[]
for name in ('LCSC3D.exe','LCSC3D.zip'):
 with (outputs/name).open('rb') as stream:hashes.append(hashlib.file_digest(stream,'sha256').hexdigest()+'  '+name)
(outputs/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n',encoding='ascii')
print('DELIVERY_VERIFIED',version)
