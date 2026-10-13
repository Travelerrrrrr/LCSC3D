"""Small public-schema fixtures; every product/account is synthetic."""


def categories():
    return {'code': 200, 'result': {'chips': {'catalogId': 470, 'catalogName': '微控制器 / 逻辑器件',
        'sonCatalogList': [{'catalogId': 470, 'catalogName': '单片机 / 微控制器', 'sonCatalogList': [
            {'catalogId': 11329, 'catalogName': '单片机(MCU/MPU/SOC)'}]}]},
        'passives': {'catalogId': 312, 'catalogName': '电容 / 电阻 / 电感', 'sonCatalogList': [
            {'catalogId': 312, 'catalogName': '电容', 'sonCatalogList': [{'catalogId': 313, 'catalogName': '贴片电容(MLCC)'}]}]}}}


def parameters(category=False):
    groups = {'param_31013_n': [{'name': '64KB', 'status': True, 'standardUnitValue': '65536'},
                              {'name': '128KB', 'status': True, 'standardUnitValue': '131072'},
                              {'name': '256KB', 'status': False, 'standardUnitValue': '262144'}]}
    info = dict(paramName='程序存储容量', isNumberRange=True, unitConversionMap={'Byte': 1, 'KB': 1024})
    if category:
        return {'paramGroupMap': {'queryProductGradePlateId': {'paramName': '品牌', 'paramDetailList': [
            {'name': 'Offline fixture', 'code': '100', 'status': True}]},
            'queryProductStandard': {'paramName': '封装', 'paramDetailList': [{'name': 'LQFN-56', 'status': True}]},
            'param_31013_n': {**info, 'paramDetailList': groups['param_31013_n']}}}
    return {'paramGroupMap': groups, 'paramNameMap': {'param_31013_n': '程序存储容量'}, 'paramInfoMap': {'param_31013_n': info}}


def add_facets(payload):
    payload['result'].update(catalogGroupJson='[{"label":"单片机(MCU/MPU/SOC)","value":"11329","count":1}]',
        gradePlateGroupJson='[{"label":"Offline fixture","value":"100","count":1}]',
        encapsulationGroupJson='[{"label":"LQFN-56","count":1}]')
    return payload
