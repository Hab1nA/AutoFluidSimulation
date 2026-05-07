import sys
print("Python:", sys.executable)
print("Version:", sys.version)

try:
    import win32com
    print("win32com: OK")
except ImportError as e:
    print("win32com: FAIL -", e)

try:
    import win32com.client
    print("win32com.client: OK")
except ImportError as e:
    print("win32com.client: FAIL -", e)

try:
    import pythoncom
    print("pythoncom: OK")
    print("pythoncom.__file__:", pythoncom.__file__)
except ImportError as e:
    print("pythoncom: FAIL -", e)

try:
    import pywintypes
    print("pywintypes: OK")
except ImportError as e:
    print("pywintypes: FAIL -", e)
