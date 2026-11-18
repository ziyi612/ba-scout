from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable,
    VarFileInfo, VarStruct, VSVersionInfo,
)


def windows_version_info(version):
    numbers = tuple(int(part) for part in version.split('.')) + (0,)
    strings = {
        'FileDescription': '断箭战前情报 - 公开记录汇总',
        'FileVersion': version,
        'InternalName': 'BA-Scout',
        'OriginalFilename': 'BA-Scout.exe',
        'ProductName': '断箭战前情报',
        'ProductVersion': version,
    }
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3f,
                          flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable('080404b0', [
            StringStruct(key, value) for key, value in strings.items()
        ])]), VarFileInfo([VarStruct('Translation', [2052, 1200])])],
    )
