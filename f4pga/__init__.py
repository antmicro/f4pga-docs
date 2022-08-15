#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2022 F4PGA Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0

"""
F4PGA Build System

This tool allows for building FPGA targets (such as bitstreams) for any supported platform with just one simple command
and a project file.

The idea is that F4PGA wraps all the tools needed by different platforms in "modules", which define inputs/outputs and
various parameters.
This allows F4PGA to resolve dependencies for any target provided that a "flow definition" file exists for such target.
The flow defeinition file list modules available for that platform and may tweak some settings of those modules.

A basic example of using F4PGA:

$ f4pga build --flow flow.json --part XC7A35TCSG324-1 -t bitstream

This will make F4PGA attempt to create a bitstream for arty_35 platform.
``flow.json`` is a flow configuration file, which should be created for a project that uses F4PGA.
Contains project-specific definitions needed within the flow, such as list of source code files.
"""

from pathlib import Path
from argparse import Namespace
from sys import argv as sys_argv
from os import environ
from yaml import load as yaml_load, Loader as yaml_loader
from typing import Iterable
from colorama import Fore, Style

from f4pga.common import (
    F4PGAException,
    ResolutionEnv,
    fatal,
    scan_modules,
    set_verbosity_level,
    sfprint,
    sub as common_sub
)
from f4pga.module import *
from f4pga.cache import F4Cache
from f4pga.flow_config import (
    ProjectFlowConfig,
    FlowConfig,
    FlowDefinition,
    open_project_flow_cfg,
    verify_platform_name
)
from f4pga.module_runner import *
from f4pga.module_inspector import get_module_info
from f4pga.stage import Stage
from f4pga.argparser import setup_argparser, get_cli_flow_config

F4CACHEPATH = '.f4cache'

install_dir = environ.get("F4PGA_INSTALL_DIR", "/usr/local")

ROOT = Path(__file__).resolve().parent

FPGA_FAM = environ.get('FPGA_FAM', 'xc7')

bin_dir_path = str(Path(sys_argv[0]).resolve().parent.parent)
share_dir_path = \
    environ.get('F4PGA_SHARE_DIR',
                str(Path(f'{install_dir}/{FPGA_FAM}/share/f4pga').resolve()))


class DependencyNotProducedException(F4PGAException):
    dep_name: str
    provider: str

    def __init__(self, dep_name: str, provider: str):
        self.dep_name = dep_name
        self.provider = provider
        self.message = f'Stage `{self.provider}` did not produce promised ' \
                       f'dependency `{self.dep_name}`'


def dep_value_str(dep: str):
    return ':' + dep


def platform_stages(platform_flow, r_env):
    """ Iterates over all stages available in a given flow. """

    stage_options = platform_flow.get('stage_options')
    for stage_name, modulestr in platform_flow['stages'].items():
        mod_opts = stage_options.get(stage_name) if stage_options else None
        yield Stage(stage_name, modulestr, mod_opts, r_env)


def req_exists(r):
    """ Checks whether a dependency exists on a drive. """

    if type(r) is str:
        if not Path(r).exists():
            return False
    elif type(r) is list:
        return not (False in map(req_exists, r))
    else:
        raise Exception(f'Requirements can be currently checked only for single '
                        f'paths, or path lists (reason: {r})')
    return True


def map_outputs_to_stages(stages: 'list[Stage]'):
    """
    Associates a stage with every possible output.
    This is commonly refferef to as `os_map` (output-stage-map) through the code.
    """

    os_map: 'dict[str, Stage]' = {} # Output-Stage map
    for stage in stages:
        for output in stage.produces:
            if not os_map.get(output.name):
                os_map[output.name] = stage
            elif os_map[output.name] != stage:
                raise Exception(f'Dependency `{output.name}` is generated by '
                                f'stage `{os_map[output.name].name}` and '
                                f'`{stage.name}`. Dependencies can have only one '
                                 'provider at most.')
    return os_map


def filter_existing_deps(deps: 'dict[str, ]', f4cache):
    return [(n, p) for n, p in deps.items() \
            if req_exists(p)] # and not dep_differ(p, f4cache)]


def get_stage_values_override(og_values: dict, stage: Stage):
    values = og_values.copy()
    values.update(stage.value_ovds)
    return values


def prepare_stage_io_input(stage: Stage):
    return { 'params': stage.params } if stage.params is not None else {}


def prepare_stage_input(stage: Stage, values: dict, dep_paths: 'dict[str, ]',
                        config_paths: 'dict[str, ]'):
    takes = {}
    for take in stage.takes:
        paths = dep_paths.get(take.name)
        if paths: # Some takes may be not required
            takes[take.name] = paths

    produces = {}
    for prod in stage.produces:
        if dep_paths.get(prod.name):
            produces[prod.name] = dep_paths[prod.name]
        elif config_paths.get(prod.name):
            produces[prod.name] = config_paths[prod.name]

    stage_mod_cfg = {
        'takes': takes,
        'produces': produces,
        'values': values
    }

    return stage_mod_cfg


def update_dep_statuses(paths, consumer: str, f4cache: F4Cache):
    if type(paths) is str:
        return f4cache.update(Path(paths), consumer)
    elif type(paths) is list:
        for p in paths:
            return update_dep_statuses(p, consumer, f4cache)
    elif type(paths) is dict:
        for _, p in paths.items():
            return update_dep_statuses(p, consumer, f4cache)
    fatal(-1, 'WRONG PATHS TYPE')


def dep_differ(paths, consumer: str, f4cache: F4Cache):
    """
    Check if a dependency differs from its last version, lack of dependency is
    treated as "differs"
    """

    if type(paths) is str:
        if not Path(paths).exists():
            return True
        return f4cache.get_status(paths, consumer) != 'same'
    elif type(paths) is list:
        return True in [dep_differ(p, consumer, f4cache) for p in paths]
    elif type(paths) is dict:
        return True in [dep_differ(p, consumer, f4cache) \
                        for _, p in paths.items()]
    return False


def dep_will_differ(target: str, paths, consumer: str,
                    os_map: 'dict[str, Stage]', run_stages: 'set[str]',
                    f4cache: F4Cache):
    """
    Check if a dependency or any of the dependencies it depends on differ from
    their last versions.
    """

    provider = os_map.get(target)
    if provider:
        return (provider.name in run_stages) or \
               dep_differ(paths, consumer, f4cache)
    return dep_differ(paths, consumer, f4cache)


def _print_unreachable_stage_message(provider: Stage, take: str):
    sfprint(0, '    Stage '
              f'`{Style.BRIGHT + provider.name + Style.RESET_ALL}` is '
               'unreachable due to unmet dependency '
              f'`{Style.BRIGHT + take.name + Style.RESET_ALL}`')


def config_mod_runctx(stage: Stage, values: 'dict[str, ]',
                      dep_paths: 'dict[str, str | list[str]]',
                      config_paths: 'dict[str, str | list[str]]'):
    config = prepare_stage_input(stage, values,
                                 dep_paths, config_paths)
    return ModRunCtx(share_dir_path, bin_dir_path, config)


def _process_dep_path(path: str, f4cache: F4Cache):
    f4cache.process_file(Path(path))
_cache_deps = deep(_process_dep_path)

class Flow:
    """ Describes a complete, configured flow, ready for execution. """

    # Dependendecy to build
    target: str
    # Values in global scope
    cfg: FlowConfig
    # dependency-producer map
    os_map: 'dict[str, Stage]'
    # Paths resolved for dependencies
    dep_paths: 'dict[str, str | list[str]]'
    # Explicit configs for dependency paths
    # config_paths: 'dict[str, str | list[str]]'
    # Stages that need to be run
    run_stages: 'set[str]'
    # Number of stages that relied on outdated version of a (checked) dependency
    deps_rebuilds: 'dict[str, int]'
    f4cache: 'F4Cache | None'
    flow_cfg: FlowConfig

    def __init__(self, target: str, cfg: FlowConfig,
                 f4cache: 'F4Cache | None'):
        self.target = target
        self.os_map = map_outputs_to_stages(cfg.stages.values())

        explicit_deps = cfg.get_dependency_overrides()
        self.dep_paths = dict(filter_existing_deps(explicit_deps, f4cache))
        if f4cache is not None:
            for dep in self.dep_paths.values():
                _cache_deps(dep, f4cache)

        self.run_stages = set()
        self.f4cache = f4cache
        self.cfg = cfg
        self.deps_rebuilds = {}

        self._resolve_dependencies(self.target, set())

    def _dep_will_differ(self, dep: str, paths, consumer: str):
        if not self.f4cache: # Handle --nocache mode
            return True
        return dep_will_differ(dep, paths, consumer,
                               self.os_map, self.run_stages,
                               self.f4cache)

    def _resolve_dependencies(self, dep: str, stages_checked: 'set[str]',
                              skip_dep_warnings: 'set[str]' = None):
        if skip_dep_warnings is None:
            skip_dep_warnings = set()

        # Initialize the dependency status if necessary
        if self.deps_rebuilds.get(dep) is None:
            self.deps_rebuilds[dep] = 0
        # Check if an explicit dependency is already resolved
        paths = self.dep_paths.get(dep)
        if paths and not self.os_map.get(dep):
            return
        # Check if a stage can provide the required dependency
        provider = self.os_map.get(dep)
        if not provider or provider.name in stages_checked:
            return

        # TODO: Check if the dependency is "on-demand" and force it in provider's
        # config if it is.

        for take in provider.takes:
            self._resolve_dependencies(take.name, stages_checked, skip_dep_warnings)
            # If any of the required dependencies is unavailable, then the
            # provider stage cannot be run
            take_paths = self.dep_paths.get(take.name)
            # Add input path to values (dirty hack)
            provider.value_overrides[dep_value_str(take.name)] = take_paths

            if not take_paths and take.spec == 'req':
                _print_unreachable_stage_message(provider, take)
                return

            will_differ = False
            if take_paths is None:
                # TODO: This won't trigger rebuild if an optional dependency got removed
                will_differ = False
            elif req_exists(take_paths):
                will_differ = self._dep_will_differ(take.name, take_paths, provider.name)
            else:
                will_differ = True


            if will_differ:
                if take.name not in skip_dep_warnings:
                    sfprint(2, f'{Style.BRIGHT}{take.name}{Style.RESET_ALL} is causing '
                               f'rebuild for `{Style.BRIGHT}{provider.name}{Style.RESET_ALL}`')
                    skip_dep_warnings.add(take.name)
                self.run_stages.add(provider.name)
                self.deps_rebuilds[take.name] += 1

        stage_values = self.cfg.get_r_env(provider.name).values
        modrunctx = config_mod_runctx(provider, stage_values, self.dep_paths,
                                      self.cfg.get_dependency_overrides())

        outputs = module_map(provider.module, modrunctx)
        for output_paths in outputs.values():
            if output_paths is not None:
                if req_exists(output_paths) and self.f4cache:
                    _cache_deps(output_paths, self.f4cache)

        stages_checked.add(provider.name)
        self.dep_paths.update(outputs)

        for _, out_paths in outputs.items():
            if (out_paths is not None) and not (req_exists(out_paths)):
                self.run_stages.add(provider.name)

        # Verify module's outputs and add paths as values.
        outs = outputs.keys()
        for o in provider.produces:
            if o.name not in outs:
                if o.spec == 'req' or (o.spec == 'demand' and \
                        o.name in self.cfg.get_dependency_overrides().keys()):
                    fatal(-1, f'Module {provider.name} did not produce a mapping '
                              f'for a required output `{o.name}`')
                else:
                    # Remove an on-demand/optional output that is not produced
                    # from os_map.
                    self.os_map.pop(o.name)
            # Add a value for the output (dirty ack yet again)
            o_path = outputs.get(o.name)

            if o_path is not None:
                provider.value_overrides[dep_value_str(o.name)] = \
                    outputs.get(o.name)


    def print_resolved_dependencies(self, verbosity: int):
        deps = list(self.deps_rebuilds.keys())
        deps.sort()

        for dep in deps:
            status = Fore.RED + '[X]' + Fore.RESET
            source = Fore.YELLOW + 'MISSING' + Fore.RESET
            paths = self.dep_paths.get(dep)

            if paths:
                exists = req_exists(paths)
                provider = self.os_map.get(dep)
                if provider and provider.name in self.run_stages:
                    if exists:
                        status = Fore.YELLOW + '[R]' + Fore.RESET
                    else:
                        status = Fore.YELLOW + '[S]' + Fore.RESET
                    source = f'{Fore.BLUE + self.os_map[dep].name + Fore.RESET} ' \
                            f'-> {paths}'
                elif exists:
                    if self.deps_rebuilds[dep] > 0:
                        status = Fore.GREEN + '[N]' + Fore.RESET
                    else:
                        status = Fore.GREEN + '[O]' + Fore.RESET
                    source = paths
            elif self.os_map.get(dep):
                status = Fore.RED + '[U]' + Fore.RESET
                source = \
                    f'{Fore.BLUE + self.os_map[dep].name + Fore.RESET} -> ???'

            sfprint(verbosity, f'    {Style.BRIGHT + status} '
                               f'{dep + Style.RESET_ALL}:  {source}')

    def _build_dep(self, dep):
        paths = self.dep_paths.get(dep)
        provider = self.os_map.get(dep)
        run = (provider.name in self.run_stages) if provider else False
        if not paths:
            sfprint(2, f'Dependency {dep} is unresolved.')
            return False

        if req_exists(paths) and not run:
            return True
        else:
            assert provider

            any_dep_differ = False if (self.f4cache is not None) else True
            for p_dep in provider.takes:
                if not self._build_dep(p_dep.name):
                    assert (p_dep.spec != 'req')
                    continue

                if self.f4cache is not None:
                    any_dep_differ |= \
                        update_dep_statuses(self.dep_paths[p_dep.name],
                                            provider.name, self.f4cache)

            # If dependencies remained the same, consider the dep as up-to date
            # For example, when changing a comment in Verilog source code,
            # the initial dependency resolution will report a need for complete
            # rebuild, however, after the synthesis stage, the generated eblif
            # will reamin the same, thus making it unnecessary to continue the
            # rebuild process.
            if (not any_dep_differ) and req_exists(paths):
                sfprint(2, f'Skipping rebuild of `'
                           f'{Style.BRIGHT + dep + Style.RESET_ALL}` because all '
                           f'of it\'s dependencies remained unchanged')
                return True

            stage_values = self.cfg.get_r_env(provider.name).values
            modrunctx = config_mod_runctx(provider, stage_values, self.dep_paths,
                                          self.cfg.get_dependency_overrides())
            module_exec(provider.module, modrunctx)

            self.run_stages.discard(provider.name)

            for product in provider.produces:
                if (product.spec == 'req') and not req_exists(paths):
                    raise DependencyNotProducedException(dep, provider.name)
                prod_paths = self.dep_paths[product.name]
                if (prod_paths is not None) and req_exists(paths) and self.f4cache:
                    _cache_deps(prod_paths, self.f4cache)

        return True

    def execute(self):
        self._build_dep(self.target)
        if self.f4cache:
            _cache_deps(self.dep_paths[self.target], self.f4cache)
            update_dep_statuses(self.dep_paths[self.target], '__target',
                                self.f4cache)
        sfprint(0, f'Target {Style.BRIGHT + self.target + Style.RESET_ALL} '
                   f'-> {self.dep_paths[self.target]}')


def display_dep_info(stages: 'Iterable[Stage]'):
    sfprint(0, 'Platform dependencies/targets:')
    longest_out_name_len = 0
    for stage in stages:
        for out in stage.produces:
            l = len(out.name)
            if l > longest_out_name_len:
                longest_out_name_len = l

    desc_indent = longest_out_name_len + 7
    nl_indentstr = '\n'
    for _ in range(0, desc_indent):
        nl_indentstr += ' '

    for stage in stages:
        for out in stage.produces:
            pname = Style.BRIGHT + out.name + Style.RESET_ALL
            indent = ''
            for _ in range(0, desc_indent - len(pname) + 3):
                indent += ' '
            specstr = '???'
            if out.spec == 'req':
                specstr = f'{Fore.BLUE}guaranteed{Fore.RESET}'
            elif out.spec == 'maybe':
                specstr = f'{Fore.YELLOW}not guaranteed{Fore.RESET}'
            elif out.spec == 'demand':
                specstr = f'{Fore.RED}on-demand{Fore.RESET}'
            pgen = f'{Style.DIM}stage: `{stage.name}`, '\
                   f'spec: {specstr}{Style.RESET_ALL}'
            pdesc = stage.meta[out.name].replace('\n', nl_indentstr)
            sfprint(0, f'    {Style.BRIGHT + out.name + Style.RESET_ALL}:'
                       f'{indent}{pdesc}{nl_indentstr}{pgen}')


def display_stage_info(stage: Stage):
    if stage is None:
        sfprint(0, f'Stage  does not exist')
        f4pga_fail()
        return

    sfprint(0, f'Stage `{Style.BRIGHT}{stage.name}{Style.RESET_ALL}`:')
    sfprint(0, f'  Module: `{Style.BRIGHT}{stage.module.name}{Style.RESET_ALL}`')
    sfprint(0, f'  Module info:')

    mod_info = get_module_info(stage.module)
    mod_info = '\n    '.join(mod_info.split('\n'))

    sfprint(0, f'    {mod_info}')


f4pga_done_str = Style.BRIGHT + Fore.GREEN + 'DONE'


def f4pga_fail():
    global f4pga_done_str
    f4pga_done_str = Style.BRIGHT + Fore.RED + 'FAILED'


def f4pga_done():
    sfprint(1, f'f4pga: {f4pga_done_str}'
               f'{Style.RESET_ALL + Fore.RESET}')
    exit(0)


def setup_resolution_env():
    """ Sets up a ResolutionEnv with default built-ins. """

    r_env = ResolutionEnv({
        'shareDir': share_dir_path,
        'binDir': bin_dir_path
    })

    def _noisy_warnings():
        """
        Emit some noisy warnings.
        """
        environ['OUR_NOISY_WARNINGS'] = 'noisy_warnings.log'
        return 'noisy_warnings.log'

    def _generate_values():
        """
        Generate initial values, available in configs.
        """
        conf = {
            'python3': common_sub('which', 'python3').decode().replace('\n', ''),
            'noisyWarnings': _noisy_warnings()
        }
        if (FPGA_FAM == 'xc7'):
            conf['prjxray_db'] = common_sub('prjxray-config').decode().replace('\n', '')

        return conf

    r_env.add_values(_generate_values())
    return r_env


def open_project_flow_config(path: str) -> ProjectFlowConfig:
    try:
        flow_cfg = open_project_flow_cfg(path)
    except FileNotFoundError as _:
        fatal(-1, 'The provided flow configuration file does not exist')
    return flow_cfg


def verify_part_stage_params(flow_cfg: FlowConfig,
                             part: 'str | None' = None):
    if part:
        platform_name = get_platform_name_for_part(part)
        if not verify_platform_name(platform_name, str(ROOT)):
            sfprint(0, f'Platform `{part}`` is unsupported.')
            return False
        if part not in flow_cfg.part():
            sfprint(0, f'Platform `{part}`` is not in project.')
            return False

    return True


def get_platform_name_for_part(part_name: str):
    """
    Gets a name that identifies the platform setup required for a specific chip.
    The reason for such distinction is that plenty of chips with different names
    differ only in a type of package they use.
    """
    with (ROOT / 'part_db.yml').open('r') as rfptr:
        for key, val in yaml_load(rfptr, yaml_loader).items():
            if part_name.upper() in val:
                return key
        raise Exception(f"Unknown part name <{part_name}>!")


def make_flow_config(project_flow_cfg: ProjectFlowConfig, part_name: str) -> FlowConfig:
    """ Create `FlowConfig` from given project flow configuration and part name """

    platform = get_platform_name_for_part(part_name)
    if platform is None:
        raise F4PGAException(
            message='You have to specify a part name or configure a default part.'
        )

    if part_name not in project_flow_cfg.parts():
        raise F4PGAException(
            message='Project flow configuration does not support requested part.'
        )

    r_env = setup_resolution_env()
    r_env.add_values({'part_name': part_name.lower()})

    scan_modules(str(ROOT))

    with (ROOT / 'platforms.yml').open('r') as rfptr:
        platforms = yaml_load(rfptr, yaml_loader)
    if platform not in platforms:
        raise F4PGAException(message=f'Flow definition for platform <{platform}> cannot be found!')

    flow_cfg = FlowConfig(
        project_flow_cfg,
        FlowDefinition(platforms[platform], r_env),
        part_name
    )

    if len(flow_cfg.stages) == 0:
        raise F4PGAException(message = 'Platform flow does not define any stage')

    return flow_cfg


def cmd_build(args: Namespace):
    """ `build` command implementation """

    project_flow_cfg: ProjectFlowConfig = None

    part_name = args.part

    if args.flow:
        project_flow_cfg = open_project_flow_config(args.flow)
    elif part_name is not None:
        project_flow_cfg = ProjectFlowConfig('.temp.flow.json')
        project_flow_cfg.flow_cfg = get_cli_flow_config(args, part_name)
    if part_name is None and project_flow_cfg is not None:
        part_name = project_flow_cfg.get_default_part()

    if project_flow_cfg is None:
        fatal(-1, 'No configuration was provided. Use `--flow`, and/or '
                  '`--part` to configure flow.')

    flow_cfg = make_flow_config(project_flow_cfg, part_name)

    if args.info:
        display_dep_info(flow_cfg.stages.values())
        f4pga_done()

    if args.stageinfo:
        display_stage_info(flow_cfg.stages.get(args.stageinfo[0]))
        f4pga_done()

    target = args.target
    if target is None:
        target = project_flow_cfg.get_default_target(part_name)
        if target is None:
            fatal(-1, 'Please specify desired target using `--target` option '
                      'or configure a default target.')

    flow = Flow(
        target=target,
        cfg=flow_cfg,
        f4cache=F4Cache(F4CACHEPATH) if not args.nocache else None
    )

    dep_print_verbosity = 0 if args.pretend else 2
    sfprint(dep_print_verbosity, '\nProject status:')
    flow.print_resolved_dependencies(dep_print_verbosity)
    sfprint(dep_print_verbosity, '')

    if args.pretend:
        f4pga_done()

    try:
        flow.execute()
    except AssertionError as e:
        raise e
    except Exception as e:
        sfprint(0, f'{e}')
        f4pga_fail()

    if flow.f4cache:
        flow.f4cache.save()


def cmd_show_dependencies(args: Namespace):
    """ `showd` command implementation """

    flow_cfg = open_project_flow_config(args.flow)

    if not verify_part_stage_params(flow_cfg, args.part):
        f4pga_fail()
        return

    platform_overrides: 'set | None' = None
    if args.platform is not None:
        platform_overrides = \
            set(flow_cfg.get_dependency_platform_overrides(args.part).keys())

    display_list = []

    raw_deps = flow_cfg.get_dependencies_raw(args.platform)

    for dep_name, dep_paths in raw_deps.items():
        prstr: str
        if (platform_overrides is not None) and (dep_name in platform_overrides):
            prstr = f'{Style.DIM}({args.platform}){Style.RESET_ALL} ' \
                    f'{Style.BRIGHT + dep_name + Style.RESET_ALL}: {dep_paths}'
        else:
            prstr = f'{Style.BRIGHT + dep_name + Style.RESET_ALL}: {dep_paths}'

        display_list.append((dep_name, prstr))

    display_list.sort(key = lambda p: p[0])

    for _, prstr in display_list:
        sfprint(0, prstr)

    set_verbosity_level(-1)


def main():
    parser = setup_argparser()
    args = parser.parse_args()

    set_verbosity_level(args.verbose - (1 if args.silent else 0))

    if args.command == 'build':
        cmd_build(args)
        f4pga_done()

    if args.command == 'showd':
        cmd_show_dependencies(args)
        f4pga_done()

    sfprint(0, 'Please use a command.\nUse `--help` flag to learn more.')
    f4pga_done()


if __name__ == '__main__':
    main()
