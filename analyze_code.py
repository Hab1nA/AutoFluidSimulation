#!/usr/bin/env python3
"""全面代码分析脚本 - 查找导入问题、未使用变量等"""
import ast
import os
import sys

def analyze_python_file(file_path):
    """分析单个 Python 文件的潜在问题"""
    issues = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # 解析为 AST
        tree = ast.parse(content, file_path)

        # 检查导入
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    imports.add(name.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module.split('.')[0])

        # 检查未定义的变量使用（简单版）
        class NameChecker(ast.NodeVisitor):
            def __init__(self):
                self.defined_names = set()
                self.used_names = set()

            def visit_FunctionDef(self, node):
                # 函数参数
                for arg in node.args.args:
                    self.defined_names.add(arg.arg)
                for arg in node.args.kwonlyargs:
                    self.defined_names.add(arg.arg)
                if node.args.vararg:
                    self.defined_names.add(node.args.vararg.arg)
                if node.args.kwarg:
                    self.defined_names.add(node.args.kwarg.arg)
                # 函数内部
                self.generic_visit(node)

            def visit_ClassDef(self, node):
                self.defined_names.add(node.name)
                self.generic_visit(node)

            def visit_Assign(self, node):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.defined_names.add(target.id)
                    elif isinstance(target, ast.Attribute):
                        pass  # 不处理属性赋值
                    elif isinstance(target, ast.Tuple) or isinstance(target, ast.List):
                        # 简单处理解包
                        for elt in ast.walk(target):
                            if isinstance(elt, ast.Name):
                                self.defined_names.add(elt.id)
                self.generic_visit(node)

            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Store):
                    self.defined_names.add(node.id)
                elif isinstance(node.ctx, ast.Load):
                    self.used_names.add(node.id)

        checker = NameChecker()
        checker.visit(tree)
        # 检查是否有未定义的名称（排除内置名称）
        import builtins
        builtin_names = set(dir(builtins))
        undefined = checker.used_names - checker.defined_names - builtin_names - imports
        for name in undefined:
            if not name.startswith('_'):  # 忽略私有变量和特殊变量
                issues.append(f"可能使用了未定义的变量: {name}")

        # 检查异常处理
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if not node.type:
                    issues.append(f"裸 except 子句在第 {node.lineno} 行")
                elif isinstance(node.type, ast.Name) and node.type.id == 'Exception':
                    pass  # 捕获所有异常是可以的
                else:
                    pass

        return issues

    except SyntaxError as e:
        return [f"语法错误: {e}"]
    except Exception as e:
        return [f"分析失败: {e}"]

def check_all_python_files(root_dir):
    """检查根目录下所有 Python 文件"""
    print("=" * 80)
    print("全面 Python 代码分析")
    print("=" * 80)
    print()

    all_issues = {}

    for dirpath, _, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith('.py'):
                full_path = os.path.join(dirpath, filename)
                # 跳过 __pycache__ 和 .git 等
                if '__pycache__' in full_path or '.git' in full_path:
                    continue

                print(f"正在分析: {os.path.relpath(full_path, root_dir)}")
                issues = analyze_python_file(full_path)
                if issues:
                    all_issues[full_path] = issues
                    for issue in issues:
                        print(f"  - {issue}")
                    print()

    print()
    print("=" * 80)
    print(f"分析完成，共发现 {len(all_issues)} 个文件有潜在问题")
    print("=" * 80)
    return all_issues

def test_imports(root_dir):
    """测试所有模块是否可以正确导入"""
    print()
    print("=" * 80)
    print("测试模块导入")
    print("=" * 80)
    print()

    # 添加根目录到路径
    if root_dir not in sys.path:
        sys.path.insert(0, root_dir)

    # 尝试导入各个模块
    modules_to_test = [
        'engine.config',
        'engine.state_manager',
        'engine.file_monitor',
        'engine.task_runner',
        'engine.scheduler',
        'engine.daemon',
        'ipc.protocol',
        'ipc.server',
        'utils.logger',
        'utils.ssh_client',
        'utils.excel_reader',
    ]

    for module_name in modules_to_test:
        print(f"正在导入: {module_name}...", end=" ")
        try:
            __import__(module_name)
            print("✓ OK")
        except ImportError as e:
            print(f"✗ 失败: {e}")
        except Exception as e:
            print(f"✗ 其他错误: {e}")

if __name__ == "__main__":
    root_dir = os.path.dirname(os.path.abspath(__file__))
    all_issues = check_all_python_files(root_dir)
    test_imports(root_dir)
