""" Computing configuration representation """


import argparse
import jinja2
import logging
import logmuse
import os
import re
import sys
import shutil

# from distutils.dir_util import copy_tree
from shutil import copyfile

from ubiquerg import is_url, is_command_callable, parse_registry_path as prp, \
                    query_yes_no

import yacman
from collections import OrderedDict
from . import __version__

DEFAULT_CONFIG_FILEPATH =  os.path.join(
        os.path.dirname(__file__),
        "templates",
        "bulker_config.yaml")

TEMPLATE_FOLDER = os.path.join(
        os.path.dirname(__file__),
        "templates")

DOCKER_EXE_TEMPLATE = os.path.join(TEMPLATE_FOLDER, "docker_executable.jinja2")
DOCKER_BUILD_TEMPLATE = os.path.join(TEMPLATE_FOLDER, "docker_build.jinja2")
SINGULARITY_EXE_TEMPLATE =  os.path.join(TEMPLATE_FOLDER, "singularity_executable.jinja2")
SINGULARITY_BUILD_TEMPLATE =  os.path.join(TEMPLATE_FOLDER, "singularity_build.jinja2")

_LOGGER = logging.getLogger(__name__)

class _VersionInHelpParser(argparse.ArgumentParser):
    def format_help(self):
        """ Add version information to help text. """
        return "version: {}\n".format(__version__) + \
               super(_VersionInHelpParser, self).format_help()


def build_argparser():
    """
    Builds argument parser.

    :return argparse.ArgumentParser
    """

    banner = "%(prog)s - manage containerized executables"
    additional_description = "\nhttps://bulker.databio.org"

    parser = _VersionInHelpParser(
            description=banner,
            epilog=additional_description)

    parser.add_argument(
            "-V", "--version",
            action="version",
            version="%(prog)s {v}".format(v=__version__))

    subparsers = parser.add_subparsers(dest="command") 

    def add_subparser(cmd, description):
        return subparsers.add_parser(
            cmd, description=description, help=description)

    subparser_messages = {
        "init": "Initialize a new bulker config file",
        "list": "List available bulker crates",
        "load": "Create a new bulker crate from a container manifest",
        "activate": "Activate a bulker crate by adding it to your PATH",
        "run": "Run a command in a crate"
    }

    sps = {}
    for cmd, desc in subparser_messages.items():
        sps[cmd] = add_subparser(cmd, desc)
        sps[cmd].add_argument(
            "-c", "--config", required=(cmd == "init"),
            help="Bulker configuration file.")

    sps["init"].add_argument(
            "-e", "--engine", choices={"docker", "singularity", }, default=None,
            help="Choose container engine. Default: 'guess'")

    for cmd in ["run", "activate", "load"]:
        sps[cmd].add_argument(
            "crate_registry_paths", metavar="crate-registry-paths", type=str,
            help="One or more comma-separated registry path strings"
            "  that identify crates (e.g. bulker/demo:1.0.0)")

        sps[cmd].add_argument(
            "-s", "--strict", action='store_true', default=False,
            help="Use strict environment (purges PATH of other commands)?")

    sps["load"].add_argument(
            "-f", "--manifest",
            help="File path to manifest. Can be a remote URL or local file.")

    sps["load"].add_argument(
            "-p", "--path",
            help="Destination path for built crate.")

    sps["load"].add_argument(
            "-b", "--build", action='store_true', default=False,
            help="Build/pull the actual containers, in addition to the"
            "executables. Default: False")    
    
    sps["run"].add_argument(
            "cmd", metavar="command", nargs=argparse.REMAINDER, 
            help="Command to run")

    sps["activate"].add_argument(
            "-e", "--echo", action='store_true', default=False,
            help="Echo command instead of running it.")

    return parser


def select_bulker_config(filepath):
    bulkercfg = yacman.select_config(
        filepath,
        "BULKERCFG",
        default_config_filepath=DEFAULT_CONFIG_FILEPATH,
        check_exist=True)
    _LOGGER.debug("Selected bulker config: {}".format(bulkercfg))
    return bulkercfg

# parse_crate_string("abc")
# parse_crate_string("abc:123")
# parse_crate_string("name/abc:123")
# parse_crate_string("http://www.databio.org")

def parse_registry_path(path, default_namespace="bulker"):
    return prp(path, defaults=[
        ("protocol", None),
        ("namespace", default_namespace),
        ("crate", None),
        ("subcrate", None),
        ("tag", "default")])


def parse_registry_paths(paths, default_namespace="bulker"):
    if "," in paths:
        paths = paths.split(",")
    elif isinstance(paths, str):
        paths = [paths]

    return [parse_registry_path(p, default_namespace) for p in paths]

def _is_writable(folder, check_exist=False, create=False):
    """
    Make sure a folder is writable.

    Given a folder, check that it exists and is writable. Errors if requested on
    a non-existent folder. Otherwise, make sure the first existing parent folder
    is writable such that this folder could be created.

    :param str folder: Folder to check for writeability.
    :param bool check_exist: Throw an error if it doesn't exist?
    :param bool create: Create the folder if it doesn't exist?
    """
    folder = folder or "."

    if os.path.exists(folder):
        return os.access(folder, os.W_OK) and os.access(folder, os.X_OK)
    elif create_folder:
        os.mkdir(folder)
    elif check_exist:
        raise OSError("Folder not found: {}".format(folder))
    else:
        _LOGGER.debug("Folder not found: {}".format(folder))
        # The folder didn't exist. Recurse up the folder hierarchy to make sure
        # all paths are writable
        return _is_writable(os.path.dirname(folder), strict_exists)


def bulker_init(config_path, template_config_path, container_engine=None):
    """
    Initialize a config file.
    
    :param str config_path: path to bulker configuration file to 
        create/initialize
    :param str template_config_path: path to bulker configuration file to 
        copy FROM
    """
    if not config_path:
        _LOGGER.error("You must specify a file path to initialize.")
        return

    if not template_config_path:
        _LOGGER.error("You must specify a template config file path.")
        return

    if not container_engine:
        check_engines = ["docker", "singularity"]
        for engine in check_engines:
            if is_command_callable(engine):
                _LOGGER.info("Guessing container engine is {}.".format(engine))
                container_engine = engine
                break  # it's a priority list, stop at the first found engine

    if config_path and not os.path.exists(config_path):
        # dcc.write(config_path)
        # Init should *also* write the templates.
        dest_folder = os.path.dirname(config_path)
        # copy_tree(os.path.dirname(template_config_path), dest_folder)
        new_template = os.path.join(os.path.dirname(config_path), os.path.basename(template_config_path))
        bulker_config = yacman.YacAttMap(filepath=template_config_path)
        _LOGGER.debug("Engine used: {}".format(container_engine))
        bulker_config.bulker.container_engine = container_engine
        if bulker_config.bulker.container_engine == "docker":
            bulker_config.bulker.executable_template = DOCKER_EXE_TEMPLATE
            bulker_config.bulker.build_template = DOCKER_BUILD_TEMPLATE
        elif bulker_config.bulker.container_engine == "singularity":
            bulker_config.bulker.executable_template = SINGULARITY_EXE_TEMPLATE
            bulker_config.bulker.build_template = SINGULARITY_BUILD_TEMPLATE        
        bulker_config.write(config_path)
        # copyfile(template_config_path, new_template)
        # os.rename(new_template, config_path)
        _LOGGER.info("Wrote new configuration file: {}".format(config_path))
    else:
        _LOGGER.warning("Can't initialize, file exists: {} ".format(config_path))


def bulker_load(manifest, cratevars, bcfg, jinja2_template, crate_path=None, build=False):
    manifest_name = cratevars['crate']
    # We store them in folder: namespace/crate/version
    if not crate_path:
        crate_path = os.path.join(bcfg.bulker.default_crate_folder,
                                  cratevars['namespace'],
                                  manifest_name,
                                  cratevars['tag'])
    _LOGGER.debug("Crate path: {}".format(crate_path))
    _LOGGER.debug("cratevars: {}".format(cratevars))
    # Update the config file
    if not bcfg.bulker.crates:
        bcfg.bulker.crates = {}
    if not hasattr(bcfg.bulker.crates, cratevars['namespace']):
        bcfg.bulker.crates[cratevars['namespace']] = yacman.YacAttMap({})
    if not hasattr(bcfg.bulker.crates[cratevars['namespace']], cratevars['crate']):
        bcfg.bulker.crates[cratevars['namespace']][cratevars['crate']] = yacman.YacAttMap({})
    if hasattr(bcfg.bulker.crates[cratevars['namespace']][cratevars['crate']], cratevars['tag']):
        _LOGGER.debug(bcfg.bulker.crates[cratevars['namespace']][cratevars['crate']].to_dict())
        if not query_yes_no("That manifest has already been loaded. Overwrite?"):
            return
        else:
            bcfg.bulker.crates[cratevars['namespace']][cratevars['crate']][str(cratevars['tag'])] = crate_path
            _LOGGER.warning("Removing all executables in: {}".format(crate_path))
            try:
                shutil.rmtree(crate_path)
            except FileNotFoundError:
                _LOGGER.error("Not found, crate moved. Remove it manually.")
    else:
        bcfg.bulker.crates[cratevars['namespace']][cratevars['crate']][str(cratevars['tag'])] = crate_path


    # Now make the crate
    os.makedirs(crate_path, exist_ok=True)
    cmdlist = []
    for pkg in manifest.manifest.commands:
        _LOGGER.debug(pkg)
        pkg = yacman.YacAttMap(pkg)  # (otherwise it's just a dict)
        pkg.update(bcfg.bulker)
        if "singularity_image_folder" in pkg:
            pkg["singularity_image"] = os.path.basename(pkg["docker_image"])
            pkg["namespace"] = os.path.dirname(pkg["docker_image"])
            pkg["singularity_fullpath"] = os.path.join(pkg["singularity_image_folder"], pkg["namespace"], pkg["singularity_image"])
            os.makedirs(os.path.dirname(pkg["singularity_fullpath"]), exist_ok=True)
        command = pkg["command"]
        path = os.path.join(crate_path, command)
        _LOGGER.debug("Writing {cmd}".format(cmd=path))
        cmdlist.append(command)
        with open(path, "w") as fh:
            fh.write(jinja2_template.render(pkg=pkg))
            os.chmod(path, 0o755)
        if build:
            buildscript = build.render(pkg=pkg)
            x = os.system(buildscript)
            if x != 0:
                _LOGGER.error("------ Error building. Build script used: ------")
                _LOGGER.error(buildscript)
                _LOGGER.error("------------------------------------------------")
            _LOGGER.info("Container available at: {cmd}".format(cmd=pkg["singularity_fullpath"]))

    _LOGGER.info("Loading manifest: '{m}'. Activate with 'bulker activate {m}'.".format(m=manifest_name))
    _LOGGER.info("Commands available: {}".format(", ".join(cmdlist)))


    bcfg.write()

def bulker_activate(bulker_config, cratelist, echo=False, strict=False):
    # activating is as simple as adding a crate folder to the PATH env var.
    newpath = get_new_PATH(bulker_config, cratelist, strict)
    _LOGGER.debug("Newpath: {}".format(newpath))
    if echo:
        print("export PATH={}".format(newpath))
    else:
        os.environ["PATH"] = newpath
        # os.system("bash")
        os.execlp("/bin/bash", "bulker")
        os._exit(-1)

def get_local_path(bulker_config, cratevars):
    """
    :param dict cratevars: dict with crate metadata returned from parse_registry_path
    :param YacAttMap bulker_config: bulker config object
    :return str: path to requested crate folder
    """
    _LOGGER.debug(cratevars)
    _LOGGER.debug(bulker_config.bulker.crates[cratevars["namespace"]][cratevars["crate"]].to_dict())

    return bulker_config.bulker.crates[cratevars["namespace"]][cratevars["crate"]][cratevars["tag"]]

def get_new_PATH(bulker_config, cratelist, strict=False):
    """
    Returns local paths to crates

    :: param str crates :: string with a comma-separated list of crate identifiers
    """

    cratepaths = ""
    for cratevars in cratelist:
        cratepaths += get_local_path(bulker_config, cratevars) + os.pathsep
    
    if strict:
        newpath = cratepaths
    else:
        newpath = cratepaths + os.pathsep + os.environ["PATH"]

    return newpath

def bulker_run(bulker_config, cratelist, command, strict=False):
    _LOGGER.debug("Running.")
    _LOGGER.debug("{}".format(command))
    newpath = get_new_PATH(bulker_config, cratelist, strict)

    os.environ["PATH"] = newpath  
    export = "export PATH=\"{}\"".format(newpath)
    merged_command = "{export}; {command}".format(export=export, command=" ".join(command))
    _LOGGER.debug("{}".format(merged_command))
    # os.system(merged_command)
    # os.execlp(command[0], merged_command)
    import subprocess
    subprocess.call(merged_command, shell=True)


def load_remote_registry_path(bulker_config, registry_path, filepath=None):
    cratevars = parse_registry_path(registry_path)
    if cratevars:
        # assemble the query string
        if 'registry_url' in bulker_config.bulker:
            base_url = bulker_config.bulker.registry_url
        else:
            # base_url = "http://bulker.io"
            base_url = "http://big.databio.org/bulker/"
        query = cratevars["crate"]
        if cratevars["tag"] != "default":
            query = query + "_" + cratevars["tag"]
        if not cratevars["namespace"]:
            cratevars["namespace"] = "bulker"  # default namespace
        query = cratevars["namespace"] + "/" + query
        # Until we have an API:
        query = query + ".yaml"

        if not filepath:
            filepath = os.path.join(base_url, query)
    else: 
        _LOGGER.error("Unable to parse registry path: {}".format(registry_path))
        sys.exit(1)

    if is_url(filepath):
        _LOGGER.info("Got URL: {}".format(filepath))
        import urllib.request
        try:
            response = urllib.request.urlopen(filepath)
        except urllib.error.HTTPError as e:
            if cratevars:
                _LOGGER.error("The requested remote manifest '{}' is not found.".format(
                    filepath))
                sys.exit(1)
            else:
                raise e
        data = response.read()      # a `bytes` object
        text = data.decode('utf-8')
        manifest_lines = yacman.YacAttMap(yamldata=text)
    else:
        manifest_lines = yacman.YacAttMap(filepath=filepath)

    return manifest_lines, cratevars


def main():
    """ Primary workflow """

    parser = logmuse.add_logging_options(build_argparser())
    args, remaining_args = parser.parse_known_args()
    logger_kwargs = {"level": args.verbosity, "devmode": args.logdev}
    logmuse.init_logger(name="yacman", **logger_kwargs)
    global _LOGGER
    _LOGGER = logmuse.logger_via_cli(args)

    _LOGGER.debug("Command given: {}".format(args.command))

    if not args.command:
        parser.print_help()
        _LOGGER.error("No command given")
        sys.exit(1)

    if args.command == "init":
        bulkercfg = args.config
        _LOGGER.debug("Initializing bulker configuration")
        _is_writable(os.path.dirname(bulkercfg), check_exist=False)
        bulker_init(bulkercfg, DEFAULT_CONFIG_FILEPATH, args.engine)
        sys.exit(0)      

    bulkercfg = select_bulker_config(args.config)
    _LOGGER.info("Bulker config: {}".format(bulkercfg))
    bulker_config = yacman.YacAttMap(filepath=bulkercfg)


    if args.command == "list":
        # Output header via logger and content via print so the user can
        # redirect the list from stdout if desired without the header as clutter
        _LOGGER.info("Available crates:")
        if bulker_config.bulker.crates:
            for namespace, crates in bulker_config.bulker.crates.items():
                for crate, tags in crates.items():
                    for tag, path in tags.items():
                        print("{}/{}:{} -- {}".format(namespace, crate, tag, path))
        else:
            _LOGGER.info("No crates available. Use 'bulker load' to load a crate.")
        sys.exit(1)

    # For all remaining commands we need a crate identifier

    if args.command == "activate":
        try:
            cratelist = parse_registry_paths(args.crate_registry_paths, bulker_config.bulker.default_namespace)
            _LOGGER.debug(cratelist)
            _LOGGER.info("Activating bulker crate: {}\n".format(args.crate_registry_paths))
            bulker_activate(bulker_config, cratelist, echo=args.echo, strict=args.strict)
        except KeyError as e:
            parser.print_help(sys.stderr)
            _LOGGER.error("{} is not an available crate".format(args.crate_registry_paths))
            sys.exit(1)

    if args.command == "run":
        try:
            cratelist = parse_registry_paths(args.crate_registry_paths)
            _LOGGER.info("Activating crate: {}\n".format(args.crate_registry_paths))
            bulker_run(bulker_config, cratelist, args.cmd, strict=args.strict)
        except KeyError as e:
            parser.print_help(sys.stderr)
            _LOGGER.error("{} is not an available crate".format(args.crate_registry_paths))
            sys.exit(1)

    if args.command == "load":
        manifest, cratevars = load_remote_registry_path(bulker_config, 
                                                        args.crate_registry_paths,
                                                        args.manifest)
        exe_template_jinja = None
        build_template_jinja = None
        exe_template = os.path.join(TEMPLATE_FOLDER, bulker_config.bulker.executable_template)
        build_template = os.path.join(TEMPLATE_FOLDER, bulker_config.bulker.build_template)
        assert(os.path.exists(exe_template))
        with open(exe_template, 'r') as f:
        # with open(DOCKER_TEMPLATE, 'r') as f:
            contents = f.read()
            exe_template_jinja = jinja2.Template(contents)

        _LOGGER.info("Executable template: {}".format(exe_template))

        if args.build:
            _LOGGER.info("Building images with template: {}".format(build_template))
            with open(build_template, 'r') as f:
                contents = f.read()
                build_template_jinja = jinja2.Template(contents)

        bulker_load(manifest, cratevars, bulker_config, exe_template_jinja, 
                    crate_path=args.path,
                    build=build_template_jinja)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        _LOGGER.error("Program canceled by user!")
        sys.exit(1)