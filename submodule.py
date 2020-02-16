#!/usr/bin/env python3
# SPDX-License-Identifier: MIT

import argparse
import configparser
import json
import pathlib
import shutil
import subprocess
import typing
import urllib.parse

GitModuleInfo = typing.Dict[str, typing.Union[str, pathlib.Path]]  # map for .gitmodules section
GitModules = typing.Dict[str, GitModuleInfo]  # canonical path -> GitModuleInfo
ZuulProject = typing.Dict[str, str]  # zuul.project map
ZuulProjects = typing.Dict[str, ZuulProject]  # zuul.projects map


def url2canonical_name(site: str) -> str:
    """Remove all schemas and ports from site and return canonical path
    >>> url2canonical_name('ssh://site.example.com:29418/foo/bar')
    'site.example.com/foo/bar'
    >>> url2canonical_name('https://site.example.com/foo/')
    'site.example.com/foo'
    >>> url2canonical_name('https://site.example.com/')
    'site.example.com'
    """
    url = urllib.parse.urlparse(site)
    # url.path contains leading slash
    assert url.hostname is not None
    return url.hostname + url.path.rstrip('/')


def resolve_submodule_url(submodule_url: str, repo_url: str) -> str:
    """Resolve submodule_url (can be absolute or relative) from repo_url. Return canonical project name.

    >>> resolve_submodule_url('../../foo', 'ssh://site.example.com:29418/top/bar', )
    'site.example.com/foo'
    >>> resolve_submodule_url('../../foo1/foo2', 'ssh://site.example.com:29418/top/bar')
    'site.example.com/foo1/foo2'
    >>> resolve_submodule_url('../../foo1/foo2', 'ssh://site.example.com/')
    Traceback (most recent call last):
    ...
    ValueError: Relative submodule_url ../../foo1/foo2 is out of repo_url ssh://site.example.com/
    >>> resolve_submodule_url('https://site.example.com/foo/bar', 'nonsignificant')
    'site.example.com/foo/bar'
    """
    if '://' in submodule_url:
        return url2canonical_name(submodule_url)
    assert(submodule_url.startswith('../'))
    path = pathlib.PurePath(url2canonical_name(repo_url))
    for dir_ in submodule_url.split('/'):
        if dir_ == '..':
            if path == path.parent:
                raise ValueError(f"Relative submodule_url {submodule_url}"
                                 f" is out of repo_url {repo_url}")
            path = path.parent
        else:
            path = path / dir_
    return str(path)


def get_remote_url(repopath: typing.Union[str, pathlib.Path],
                   remote: str = 'origin') -> str:
    result = subprocess.run(
        ['git', '-C', repopath, 'remote', 'get-url', remote],
        check=True, universal_newlines=True,
        stdout=subprocess.PIPE)
    return result.stdout


def parse_gitmodules(gitmodules: pathlib.Path) -> GitModules:
    """Return dict of 'canonical_name': 'module' mapping.
    module is a dict describing submodule.
    """
    cfg = configparser.ConfigParser()
    read_ok = cfg.read(gitmodules)
    if not read_ok:  # failed to read file
        return {}
    modules = {}
    remote = get_remote_url(gitmodules.parent)
    for section in cfg.sections():
        module: GitModuleInfo = \
            {'path': cfg[section]['path'],
             'abspath': (gitmodules.parent / cfg[section]['path']).resolve(),
             'submodule': section.split()[-1].replace('"', '')}
        branch = cfg.get(section, 'branch', fallback=None)
        if branch is not None:
            module['branch'] = branch
        modules[resolve_submodule_url(cfg[section]['url'], remote)] = module
    return modules


def split_modules(modules: GitModules,
                  projects: ZuulProjects) -> typing.Tuple[GitModules, GitModules]:
    """Return canonical names to replace and canonical names to be checked out
    from remote"""
    project_cnames = set(projects.keys())
    module_cnames = set(modules.keys())

    to_replace_cnames = project_cnames & module_cnames
    to_clone_cnames = module_cnames - project_cnames

    return ({cname: modules[cname] for cname in to_replace_cnames},
            {cname: modules[cname] for cname in to_clone_cnames})


def print_split_modules(modules_to_replace: GitModules,
                        modules_to_clone: GitModules,
                        super_project: ZuulProject):
    if len(modules_to_replace):
        print(f"Following submodules of {super_project['canonical_name']}"
              " will be replaced with zuul projects:")
        for module_cname, module in modules_to_replace.items():
            branch = f"branch {module['branch']}" if 'branch' in module else ''
            print(f"* {module['abspath']} ⇒ {module_cname} {branch}")

    if len(modules_to_clone):
        print(f"Following submodules of {super_project['canonical_name']}"
              f" will be cloned:")
        for module_cname, module in modules_to_clone.items():
            branch = f"branch {module['branch']}" if 'branch' in module else ''
            print(f"* {module['abspath']} from {module_cname} {branch}")


def update_submodule(repo_path: typing.Union[str, pathlib.Path],
                     recursive: bool,
                     submodule_path: typing.Union[str, pathlib.Path] = None):
    cmd = ['git', '-C', repo_path, 'submodule', 'update', '--init']
    if recursive:
        cmd.append('--recursive')
    if submodule_path is not None:
        cmd.extend(['--', submodule_path])
    subprocess.run(cmd, check=True, universal_newlines=True)


def update_projects(projects: ZuulProjects, recursive: bool, dry_run=False, ):
    for project in projects.values():
        if not (pathlib.Path(project['src_dir']) / '.gitmodules').exists():
            print(f"{project['canonical_name']}: no .gitmodules found")
            continue
        print(f"{project['canonical_name']}: cloning submodules", flush=True)
        if dry_run:
            continue
        update_submodule(project['src_dir'], recursive)


def update_super_project(super_project: ZuulProject,
                         projects: ZuulProjects,
                         recursive: bool,
                         dry_run=False,
                         verbose=False):
    """Replace super project submodules with corresponding Zuul projects.
    Others super project submodules are cloned from origin."""
    modules = parse_gitmodules(pathlib.Path(super_project['src_dir']) / '.gitmodules')
    if not modules:
        print(f'No .gitmodules found in super project {super_project["src_dir"]}')
        return

    modules_to_replace, modules_to_clone = split_modules(modules, projects)
    if verbose:
        print_split_modules(modules_to_replace, modules_to_clone, super_project)

    for module_cname, module in modules_to_replace.items():
        src_dir = projects[module_cname]['src_dir']
        src_absdir = pathlib.Path(src_dir).resolve()
        module_abspath = pathlib.Path(module['abspath'])

        branch = f"branch {module['branch']}" if 'branch' in module else ''
        print(f'Replace submodule {module_abspath} with {src_dir} {branch}')
        if not dry_run:
            module_abspath.rmdir()
            shutil.move(src_absdir, module_abspath)

            if 'branch' not in module:
                continue

            print(f'Checkout submodule: {module_abspath}')
            subprocess.run(['git', '-C', super_project['src_dir'],
                            'submodule', 'init', module['path']],
                           check=True)
            subprocess.run(['git', '-C', super_project['src_dir'],
                            'submodule', 'absorbgitdirs', module['path']],
                           check=True)
            # zuul checkouts the same branches for dependent projects as for main
            # project's branch, so we have to checkout required branch
            subprocess.run(['git', '-C', module_abspath, 'checkout', module['branch']],
                           check=True)
            # If there are submodules within this submodule, they have to be updated
            # without updating the submodule itself.
            if recursive:
                update_submodule(module_abspath, recursive)

    for module_cname, module in modules_to_clone.items():
        branch = f"branch {module['branch']}" if 'branch' in module else ''
        # cloning can be time consuming, so lets flush printing
        print(f"Cloning submodule {module['abspath']} from {module_cname} {branch}",
              flush=True)
        if dry_run:
            continue
        update_submodule(super_project['src_dir'], recursive, module['path'])


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('zuul_json', type=argparse.FileType(),
                        help='JSON file with zuul variable')
    parser.add_argument('super_project',
                        help='super project canonical name (e.g. example.com/foo/bar')
    parser.add_argument('--dry-run', action='store_true',
                        help="don't touch repositories")
    parser.add_argument('--recursive', action='store_true',
                        help="update submodules recursively")
    parser.add_argument('--verbose', action='store_true',
                        help="print more info")
    return parser.parse_args()


def main():
    args = parse_args()
    zuul = json.load(args.zuul_json)
    super_project = zuul['projects'][args.super_project]

    # create projects that doesn't contain super project
    projects = zuul['projects'].copy()  # it's ok to do shallow copy here
    projects.pop(super_project['canonical_name'])

    # BUG: use case when projects' submodules must be updated from siblings projects
    # is not supported
    update_projects(projects, args.recursive, args.dry_run)
    update_super_project(super_project, projects, args.recursive, args.dry_run, args.verbose)


if __name__ == '__main__':
    main()
