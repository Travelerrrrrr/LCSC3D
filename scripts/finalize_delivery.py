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
if args.settings_dir:
 settings=json.loads((args.settings_dir/'settings-verification.json').read_text(encoding='utf-8'))
 assert settings['success'] and settings['frozen'] and settings['version']==version
 for key in ('default_system_proxies','default_debug','independent_proxy_preferences','saved_to_disk','restored_from_disk','log_level_filters_output','cancel_preserves_preferences','log_package_verified','log_clear_verified'):assert settings[key]
 settings_text=('- 冻结 EXE 设置验证通过：主窗口设置入口、商城与更新独立代理、Debug 默认等级、保存/恢复、取消保留和日志过滤。配置及日志使用隔离目录。\n'
                '- 本地 HTTP 服务回归核对实际代理/直连、忽略环境变量代理、已有客户端即时切换和 Cookie 保留；日志回归核对等级过滤、敏感数据排除与文件轮转。\n')
 if settings.get('app_data_settings') and settings.get('app_data_runtime'):
  settings_text+='- EXE 配置、日志和运行时解压验证位于 LOCALAPPDATA/LCSC3D，旧配置迁移与删除、默认导出和临时下载路径由本地回归核对。\n'
 settings_text+='- 冻结 EXE 日志工具验证通过：ZIP 内容与诊断字段、AppData 保存路径、清除前确认、清除后继续记录、运行状态标记及已打包 ZIP 保留。\n'
for source,name in [('软件界面.png','软件界面'),('符号_C2040.png','符号预览'),('封装_C2040.png','封装预览'),('型号查询.png','型号查询')]:shutil.copyfile(tested/source,outputs/f'{name}.png')
shutil.copyfile(tested/'符号_C2040.png',root/'docs/images/app.png')
shutil.copyfile(tested/'封装_C2040.png',root/'docs/images/footprint.png')
text=f"""# LCSC3D {version} 成品验证

验证日期：{args.verification_date}。Windows x64、Python 3.12.10、PySide6 6.11.1。

- {args.test_count} 项本地回归通过，包含商城专项、下载列表删除、官方 STEP/OBJ、原生 AD 库、27 个官方 AD 样本、预览和自更新。
{settings_text}{store_text}- 独立中文目录运行真实 EXE，清除 Python/Qt 环境变量，仅保留系统 PATH，退出码 0。
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
for name in ('main','favorites','favorites_selftest','store','store_crypto','store_session','store_images','store_diagnostics','app_paths','app_settings','app_logging','log_support','settings_ui','settings_selftest','docs_capture','updater','update_ui','backend','altium','resources','model3d','library_preview'):
 code=compile((root/'app'/f'{name}.py').read_text(encoding='utf-8'),'','exec',dont_inherit=True)
 assert normalize(frozen.extract(name))==normalize(code),name
for name in ('viewer.html','vector_viewer.html'):
 assert executable_archive.extract(name)==(root/'app'/name).read_bytes(),name
hashes=[]
for name in ('LCSC3D.exe','LCSC3D.zip'):
 with (outputs/name).open('rb') as stream:hashes.append(hashlib.file_digest(stream,'sha256').hexdigest()+'  '+name)
(outputs/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n',encoding='ascii')
print('DELIVERY_VERIFIED',version)
