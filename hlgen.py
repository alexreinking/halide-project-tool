#!/usr/bin/env python3
import argparse
import ast
import glob
import os
import re
import sys
from pathlib import Path
from typing import Union

TOOL_DIR = Path(os.path.dirname(os.path.realpath(__file__)))
PROJ_DIR = Path(os.path.realpath(os.getcwd()))


class BuildConfig(object):
    def __init__(self, generator, config_name, value, *, source=None):
        self.gen = generator or ''
        self.subcfg = config_name or ''
        self.val = value or ''
        self.orig = source

    @staticmethod
    def from_makefile(cfg):
        # Is there a way to do this without lazy groups?
        m = re.match(r'^CFG__(\w+?)(?:\s|__(\w+)?).*?=\s*(.*?)\s*$', cfg)
        if not m:
            return None
        generator, config_name, value = m.group(1, 2, 3)
        return BuildConfig(generator, config_name, value, source=cfg)

    def __repr__(self):
        return f'({self.gen}, {self.subcfg}) = {self.val} [{(self.orig or "(none)").strip()}]'


class ProjectMakefile(object):
    def __init__(self, path: Union[Path, str]):
        if isinstance(path, str):
            path = Path(path)
        self.path = path

        with open(path, 'r') as f:
            self._lines = f.readlines()

        self._gens, self._invalid = self._parse_makefile()

    def lines(self):
        return self._lines

    def get_generators(self):
        return self._gens, self._invalid

    def _parse_makefile(self):
        saw_generator_comment = False
        num_lines = len(self._lines)
        after_comment = num_lines
        cfg_start = num_lines
        cfg_end = num_lines

        configurations = []
        invalid_configurations = []
        generator2configs = {}

        # Create table for all the valid generators
        for gen in glob.glob(str(PROJ_DIR / '*.gen.cpp')):
            gen = os.path.basename(gen).rstrip('.gen.cpp')
            generator2configs[gen] = {}

        last_cfg = num_lines

        # Populate table with configurations from makefile
        for i, line in enumerate(self._lines):
            if not line.strip():
                if saw_generator_comment and after_comment == num_lines:
                    after_comment = i
                continue

            if line.startswith('# Configure generators'):
                saw_generator_comment = True
            if saw_generator_comment and after_comment == num_lines and not line.strip().startswith('#'):
                after_comment = i

            config = BuildConfig.from_makefile(line)
            if cfg_start == num_lines and config:
                cfg_start = i

            if cfg_end == num_lines and config:
                if config.gen not in generator2configs:
                    warn(f'invalid configuration specified for {config.gen} in Makefile:{i + 1}')
                    invalid_configurations.append(config)
                else:
                    if config.subcfg in generator2configs[config.gen]:
                        warn(f'using overriding configuration for {config.gen} from Makefile:{i + 1}')
                        old_config = generator2configs[config.gen][config.subcfg]
                        configurations.remove(old_config)
                        invalid_configurations.append(old_config)
                    generator2configs[config.gen][config.subcfg] = config
                    configurations.append(config)
                last_cfg = i

            if cfg_start < num_lines and cfg_end == num_lines and not config:
                cfg_end = last_cfg + 1

        # If any generators didn't appear in the makefile, they get default configurations inferred
        for gen in generator2configs:
            if not generator2configs[gen]:
                generator2configs[gen][''] = BuildConfig(gen, '', '')
                configurations.append(BuildConfig(gen, '', ''))

        self.after_comment = after_comment
        self.cfg_start = cfg_start
        self.cfg_end = cfg_end

        print(after_comment + 1, cfg_start + 1, cfg_end + 1)

        return configurations, invalid_configurations


def warn(msg):
    print(f'WARNING: {msg}', file=sys.stderr)


def expand_template(template, env=None, **kwargs):
    if not env:
        env = dict()
    env = {k.lower(): v for k, v in env.items()}
    env.update({k.lower(): v for k, v in kwargs.items()})

    funs = {}

    def get_names(expr):
        names = set()
        for node in ast.walk(expr):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                node.id = node.id.lower()
                names.add(node.id)
        return list(names)

    def mkfun(expression):
        key = ast.dump(expression)
        if key in funs:
            return funs[key]

        names = get_names(expression)
        body = expression.body

        args = [ast.arg(arg=name, annotation=None) for name in names]
        body = ast.Lambda(
            ast.arguments(args=args, defaults=[], kwonlyargs=[], kw_defaults=[]),
            body)

        expression.body = body
        ast.fix_missing_locations(expression)

        f = compile(expression, filename="<ast>", mode='eval')
        value = (eval(f), names)
        funs[key] = value
        return value

    def expand(src):
        ex = ast.parse(src, mode='eval')
        if not isinstance(ex, ast.Expression):
            return ''
        f, names = mkfun(ex)
        args = [str(env.get(name)) or '' for name in names]
        return f(*args)

    # TODO: do something smarter here for matching { }
    return re.sub(r'\${([^}]+)}', lambda m: expand(m.group(1)), template)


def get_halide_directory():
    hldir = os.environ.get('HALIDE_DISTRIB_PATH')
    if not hldir:
        hldir = '/opt/halide'
    return os.path.realpath(hldir) if os.path.isdir(hldir) else None


class Table(object):
    def __init__(self, width=0, colpadding=1):
        self.width = width
        self.sizes = [0] * width
        self.rows = []
        self.colpadding = colpadding

    def add_row(self, *args):
        if not self.width:
            self.width = len(args)
            self.sizes = [0] * self.width
        if len(args) == 0:
            self.rows.append(tuple([''] * self.width))
            return
        if len(args) != self.width:
            raise ValueError('arguments list not the same width')
        self.sizes = list(map(max, zip(self.sizes, map(len, args))))
        self.rows.append(tuple(args))

    def __str__(self):
        gutter = ' ' * self.colpadding

        template = ['{:<{}}'] * self.width
        template = (gutter + '|' + gutter).join(template)

        output = ''
        for row in self.rows:
            fmtargs = zip(row, self.sizes)
            output += template.format(*[x for y in fmtargs for x in y]) + '\n'
        return output


class HLGen(object):
    def __init__(self):
        parser = argparse.ArgumentParser(
            description='Creates and manages Halide projects',
            usage='''hlgen <command> [<args>]

The available hlgen commands are:
   create     Create a new Halide project, generator, or configuration
   delete     Remove an existing generator or configuration
   list       List generators and their configurations
''')
        parser.add_argument('command', help='Subcommand to run')

        args = parser.parse_args(sys.argv[1:2])
        if not hasattr(self, args.command):
            parser.print_help()
            sys.exit(1)

        getattr(self, args.command)(sys.argv[2:])

    def _require_project(self):
        if not (PROJ_DIR / 'Makefile').exists():
            warn('There is no makefile in this directory. Are you sure you are in a project folder?')
            sys.exit(1)

    def list(self, argv):
        self._require_project()
        project = ProjectMakefile(PROJ_DIR / 'Makefile')
        configurations, invalid = project.get_generators()

        table = Table()
        for config in configurations:
            table.add_row(config.gen,
                          config.subcfg or '(default)',
                          config.val or '(default)')
        print(table)

    def create(self, argv):
        parser = argparse.ArgumentParser(
            description='Create a new Halide project, generator, or configuration',
            usage='hlgen create <item_type> [<args>]')
        parser.add_argument('item_type', type=str, choices=['project', 'generator', 'configuration'],
                            help='what kind of item to create')

        args = parser.parse_args(argv[:1])
        method = f'create_{args.item_type}'

        getattr(self, method)(argv[1:])

    def create_project(self, argv):
        parser = argparse.ArgumentParser(
            description='Create a new Halide project',
            usage='hlgen create project <name>')
        parser.add_argument('project_name', type=str,
                            help='The name of the project. This will also be the name of the directory created.')

        args = parser.parse_args(argv)

        if os.path.isdir(args.project_name):
            warn('project directory already exists!')
            sys.exit(1)

        os.mkdir(args.project_name)
        PROJ_DIR = Path(os.path.realpath(args.project_name))

        env = {'_HLGEN_BASE': TOOL_DIR,
               'NAME': args.project_name}

        self.init_from_skeleton(PROJ_DIR, env)

    def init_from_skeleton(self, project_path, env):
        skeleton_path = TOOL_DIR / 'skeleton'
        for root, _, files in os.walk(skeleton_path):
            skel_dir = Path(root)
            relative = skel_dir.relative_to(skeleton_path)
            proj_dir = project_path / relative

            os.makedirs(proj_dir, exist_ok=True)

            for file_name in files:
                with open(skel_dir / file_name, 'r') as f:
                    content = f.read()

                file_name = expand_template(file_name, env)
                with open(proj_dir / file_name, 'w') as f:
                    f.write(expand_template(content, env))


if __name__ == '__main__':
    HLGen()
