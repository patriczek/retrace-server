import grp
import time
import sys
sys.path.insert(0, "/usr/share/retrace-server/")
from retrace import *
from plugins import *


class RetraceWorker(object):
    def __init__(self, task):
        self.task = task
        self.logging_handler = None

    def begin_logging(self):
        if self.logging_handler is None:
            self.logging_handler = logging.FileHandler(
                self.task._get_file_path(RetraceTask.LOG_FILE))
        logger.addHandler(self.logging_handler)

    def end_logging(self):
        if self.logging_handler is not None:
            logger.removeHandler(self.logging_handler)

    def hook_universal(self, hook):
        """Called by the default hook implementations"""
        if hook in HOOK_SCRIPTS:
            script = HOOK_SCRIPTS[hook].format(
                hook_name=hook, task_id=self.task.get_taskid(),
                task_dir=self.task.get_savedir())
            log_info("Running hook {0} '{1}'".format(hook, script))
            child = Popen(script, shell=True, stdout=PIPE, stderr=PIPE)
            (out, err) = child.communicate()
            if out:
                log_info(out)
            if err:
                log_error(err)
            child.wait()

    def hook_pre_start(self):
        """When self.start() is called"""
        self.hook_universal("pre_start")

    def hook_start(self):
        """When task type is determined and the main task starts"""
        self.hook_universal("start")

    def hook_pre_prepare_debuginfo(self):
        """Before the preparation of debuginfo packages"""
        self.hook_universal("pre_prepare_debuginfo")

    def hook_post_prepare_debuginfo(self):
        """After the preparation of debuginfo packages"""
        self.hook_universal("post_prepare_debuginfo")

    def hook_pre_prepare_mock(self):
        """Before the preparation of mock environment"""
        self.hook_universal("pre_prepare_mock")

    def hook_post_prepare_mock(self):
        """After the preparation of mock environment"""
        self.hook_universal("post_prepare_mock")

    def hook_pre_retrace(self):
        """Before starting of the retracing itself"""
        self.hook_universal("pre_retrace")

    def hook_post_retrace(self):
        """After retracing is done"""
        self.hook_universal("post_retrace")

    def hook_success(self):
        """After retracing success"""
        self.hook_universal("success")

    def hook_fail(self, errorcode):
        """After retracing fails"""
        self.hook_universal("fail")

    def hook_pre_remove_task(self):
        """Before removing task"""
        self.hook_universal("pre_remove_task")

    def hook_post_remove_task(self):
        """After removing task"""
        self.hook_universal("post_remove_task")

    def hook_pre_clean_task(self):
        """Before cleaning task"""
        self.hook_universal("pre_clean_task")

    def hook_post_clean_task(self):
        """After cleaning task"""
        self.hook_universal("post_clean_task")

    def _fail(self, errorcode=1):
        task = self.task
        task.set_status(STATUS_FAIL)

        if CONFIG["EmailNotify"] and task.has_notify():
            try:
                log_info("Sending e-mail to %s" % ", ".join(task.get_notify()))

                message = "The task #%d on %s failed\n\n" % (task.get_taskid(), os.uname()[1])

                if task.has_url():
                    message += "URL: %s\n" % task.get_url()

                message += "Task directory: %s\n" % task.get_savedir()

                if task.has_started_time():
                    message += "Started: %s\n" % datetime.datetime.fromtimestamp(task.get_started_time())

                if task.has_remote() or task.has_downloaded():
                    files = ""
                    if task.has_remote():
                        remote = map(lambda x: x[4:] if x.startswith("FTP ") else x, task.get_remote())
                        files = ", ".join(remote)

                    if task.has_downloaded():
                        files = ", ".join(filter(None, [task.get_downloaded(), files]))

                    message += "Remote file(s): %s\n" % files

                if taks.has_log():
                    message += "\nError log:\n%s\n" % task.get_log()

                send_email("Retrace Server <%s>" % CONFIG["EmailNotifyFrom"],
                           task.get_notify(),
                           "Retrace Task #%d on %s failed" % (task.get_taskid(), os.uname()[1]),
                           message)

            except Exception as ex:
                log_error("Failed to send e-mail: %s" % ex)

        task.set_finished_time(int(time.time()))

        self.stats["duration"] = int(time.time()) - self.stats["starttime"]
        try:
            save_crashstats(self.stats)
        except Exception as ex:
            log_warn("Failed to save crash statistics: %s" % str(ex))

        if not task.get_type() in [TASK_DEBUG, TASK_RETRACE_INTERACTIVE, TASK_VMCORE_INTERACTIVE]:
            self.clean_task()

        self.hook_fail(errorcode)

        raise RetraceWorkerError(errorcode=errorcode)

    def _retrace_run(self, errorcode, cmd):
        "Runs cmd using subprocess.Popen and kills script with errorcode on failure"
        try:
            child = Popen(cmd, stdout=PIPE, stderr=STDOUT)
            output = child.communicate()[0]
        except Exception as ex:
            child = None
            log_error("An unhandled exception occured: %s" % ex)

        if not child or child.returncode != 0:
            log_error("%s exitted with %d\n=== OUTPUT ===\n%s" % (" ".join(cmd), child.returncode, output))
            self._fail(errorcode)

        return output

    def start_retrace(self, custom_arch=None):
        self.hook_start()

        task = self.task
        crashdir = os.path.join(task.get_savedir(), "crash")
        corepath = os.path.join(crashdir, "coredump")

        try:
            self.stats["coresize"] = os.path.getsize(corepath)
        except:
            pass

        if custom_arch:
            log_debug("Using custom architecture: %s" % custom_arch)
            arch = custom_arch
        else:
            # read architecture from coredump
            arch = guess_arch(corepath)

            if not arch:
                log_error("Unable to determine architecture from coredump")
                self._fail()

            log_debug("Determined architecture: %s" % arch)

        self.stats["arch"] = arch

        # read package file
        try:
            with open(os.path.join(crashdir, "package"), "r") as package_file:
                crash_package = package_file.read(ALLOWED_FILES["package"])
        except Exception as ex:
            loging.error("Unable to read crash package from 'package' file: %s" % ex)
            self._fail()

        # read package file
        if not INPUT_PACKAGE_PARSER.match(crash_package):
            log_error("Invalid package name: %s" % crash_package)
            self._fail()

        pkgdata = parse_rpm_name(crash_package)
        if not pkgdata["name"]:
            log_error("Unable to parse package name: %s" % crash_package)
            self._fail()

        self.stats["package"] = pkgdata["name"]
        if pkgdata["epoch"] != 0:
            self.stats["version"] = "%s:%s-%s" % (pkgdata["epoch"], pkgdata["version"], pkgdata["release"])
        else:
            self.stats["version"] = "%s-%s" % (pkgdata["version"], pkgdata["release"])

        # read release, distribution and version from release file
        release_path = None
        rootdir = None
        rootdir_path = os.path.join(crashdir, "rootdir")
        if os.path.isfile(rootdir_path):
            with open(rootdir_path, "r") as rootdir_file:
                rootdir = rootdir_file.read(ALLOWED_FILES["rootdir"])

            exec_path = os.path.join(crashdir, "executable")
            with open(exec_path, "r") as exec_file:
                executable = exec_file.read(ALLOWED_FILES["executable"])

            if executable.startswith(rootdir):
                with open(exec_path, "w") as exec_file:
                    exec_file.write(executable[len(rootdir):])

            rel_path = os.path.join(crashdir, "os_release_in_rootdir")
            if os.path.isfile(rel_path):
                release_path = rel_path

        if not release_path:
            release_path = os.path.join(crashdir, "os_release")
            if not os.path.isfile(release_path):
                release_path = os.path.join(crashdir, "release")

        release = "Unknown Release"
        try:
            with open(release_path, "r") as release_file:
                release = release_file.read(ALLOWED_FILES["os_release"])

            version = distribution = None
            for plugin in PLUGINS:
                match = plugin.abrtparser.match(release)
                if match:
                    version = match.group(1)
                    distribution = plugin.distribution
                    break

            if not version or not distribution:
                raise Exception("Unknown release '%s'" % release)

        except Exception as ex:
            log_error("Unable to read distribution and version from 'release' file: %s" % ex)
            log_info("Trying to guess distribution and version")
            distribution, version = guess_release(crash_package, PLUGINS)
            if distribution and version:
                log_info("%s-%s" % (distribution, version))
            else:
                log_error("Failure")
                self._fail()

        if "rawhide" in release.lower():
            version = "rawhide"

        releaseid = "%s-%s-%s" % (distribution, version, arch)
        if not releaseid in get_supported_releases():
            log_error("Release '%s' is not supported" % releaseid)
            self._fail()

        if not is_package_known(crash_package, arch, releaseid):
            log_error("Package '%s.%s' was not recognized.\nIs it a part of "
                      "official %s repositories?" % (crash_package, arch, release))
            self._fail()

        self.hook_pre_prepare_debuginfo()

        packages = [crash_package]
        missing = []
        fafrepo = ""

        packagesfile = os.path.join(crashdir, "packages")
        if os.path.isfile(packagesfile):
            with open(packagesfile, "r") as f:
                packages = f.read.split()
        elif CONFIG["UseFafPackages"]:
            packages = ["bash", "cpio", "glibc-debuginfo"]
            child = Popen(["/usr/bin/faf-c2p", "--hardlink-dir", CONFIG["FafLinkDir"],
                           os.path.join(crashdir, "coredump")], stdout=PIPE, stderr=PIPE)
            stdout, stderr = child.communicate()
            fafrepo = stdout.strip()
            if stderr:
                log_warn(stderr)

            # hack - use latest glibc - for some reason gives better results
            for filename in os.listdir(fafrepo):
                if filename.startswith("glibc"):
                    os.unlink(os.path.join(fafrepo, filename))
        else:
            # read required packages from coredump
            try:
                repoid = "%s%s" % (REPO_PREFIX, releaseid)
                yumcfgpath = os.path.join(task.get_savedir(), "yum.conf")
                with open(yumcfgpath, "w") as yumcfg:
                    yumcfg.write("[%s]\n" % repoid)
                    yumcfg.write("name=%s\n" % releaseid)
                    yumcfg.write("baseurl=file://%s/%s/\n" % (CONFIG["RepoDir"], releaseid))
                    yumcfg.write("failovermethod=priority\n")

                child = Popen(["coredump2packages", os.path.join(crashdir, "coredump"),
                               "--repos=%s" % repoid, "--config=%s" % yumcfgpath,
                               "--log=%s" % os.path.join(task.get_savedir(), "c2p_log")],
                              stdout=PIPE, stderr=PIPE)
                section = 0
                crash_package_or_component = None
                stdout, stderr = child.communicate()
                lines = stdout.split("\n")
                libdb = False
                for line in lines:
                    if line == "":
                        section += 1
                        continue
                    elif 0 == section:
                        crash_package_or_component = line.strip()
                    elif 1 == section:
                        stripped = line.strip()

                        # hack - help to depsolver, yum would fail otherwise
                        if distribution == "fedora" and stripped.startswith("gnome"):
                            packages.append("desktop-backgrounds-gnome")

                        # hack - libdb-debuginfo and db4-debuginfo are conflicting
                        if distribution == "fedora" and \
                           (stripped.startswith("db4-debuginfo") or \
                            stripped.startswith("libdb-debuginfo")):
                            if libdb:
                                continue
                            else:
                                libdb = True

                        packages.append(stripped)
                    elif 2 == section:
                        soname, buildid = line.strip().split(" ", 1)
                        if not soname or soname == "-":
                            soname = None
                        missing.append((soname, buildid))

                if stderr:
                    log_warn(stderr)

            except Exception as ex:
                log_error("Unable to obtain packages from 'coredump' file: %s" % ex)
                self._fail()

        self.hook_post_prepare_debuginfo()
        self.hook_pre_prepare_mock()

        # create mock config file
        try:
            with open(os.path.join(task.get_savedir(), RetraceTask.MOCK_DEFAULT_CFG), "w") as mockcfg:
                mockcfg.write("config_opts['root'] = '%d'\n" % task.get_taskid())
                mockcfg.write("config_opts['target_arch'] = '%s'\n" % arch)
                mockcfg.write("config_opts['chroot_setup_cmd'] = '--skip-broken install %s abrt-addon-ccpp shadow-utils gdb rpm'\n" % " ".join(packages))
                mockcfg.write("config_opts['plugin_conf']['ccache_enable'] = False\n")
                mockcfg.write("config_opts['plugin_conf']['yum_cache_enable'] = False\n")
                mockcfg.write("config_opts['plugin_conf']['root_cache_enable'] = False\n")
                mockcfg.write("config_opts['plugin_conf']['bind_mount_enable'] = True\n")
                mockcfg.write("config_opts['plugin_conf']['bind_mount_opts'] = { 'create_dirs': True,\n")
                mockcfg.write("    'dirs': [\n")
                mockcfg.write("              ('%s', '/var/spool/abrt/crash'),\n" % crashdir)
                if CONFIG["UseFafPackages"]:
                    mockcfg.write("              ('%s', '/packages'),\n" % fafrepo)
                mockcfg.write("            ] }\n")
                mockcfg.write("\n")
                mockcfg.write("config_opts['yum.conf'] = \"\"\"\n")
                mockcfg.write("[main]\n")
                mockcfg.write("cachedir=/var/cache/yum\n")
                mockcfg.write("debuglevel=1\n")
                mockcfg.write("reposdir=%s\n" % os.devnull)
                mockcfg.write("logfile=/var/log/yum.log\n")
                mockcfg.write("retries=20\n")
                mockcfg.write("obsoletes=1\n")
                if version != "rawhide" and CONFIG["RequireGPGCheck"]:
                    mockcfg.write("gpgcheck=1\n")
                else:
                    mockcfg.write("gpgcheck=0\n")
                mockcfg.write("assumeyes=1\n")
                mockcfg.write("syslog_ident=mock\n")
                mockcfg.write("syslog_device=\n")
                mockcfg.write("\n")
                mockcfg.write("#repos\n")
                mockcfg.write("\n")
                mockcfg.write("[%s]\n" % distribution)
                mockcfg.write("name=%s\n" % releaseid)
                mockcfg.write("baseurl=file://%s/%s/\n" % (CONFIG["RepoDir"], releaseid))
                mockcfg.write("failovermethod=priority\n")
                if version != "rawhide" and CONFIG["RequireGPGCheck"]:
                    mockcfg.write("gpgkey=file:///usr/share/retrace-server/gpg/%s-%s\n" % (distribution, version))
                mockcfg.write("\"\"\"\n")

            # symlink defaults from /etc/mock
            os.symlink("/etc/mock/site-defaults.cfg",
                       os.path.join(task.get_savedir(), RetraceTask.MOCK_SITE_DEFAULTS_CFG))
            os.symlink("/etc/mock/logging.ini",
                       os.path.join(task.get_savedir(), RetraceTask.MOCK_LOGGING_INI))
        except Exception as ex:
            log_error("Unable to create mock config file: %s" % ex)
            self._fail()

        # run retrace
        task.set_status(STATUS_INIT)
        log_info(STATUS[STATUS_INIT])

        self._retrace_run(25, ["/usr/bin/mock", "init", "--resultdir", task.get_savedir() + "/log", "--configdir", task.get_savedir()])

        self.hook_post_prepare_mock()
        self.hook_pre_retrace()

        if CONFIG["UseFafPackages"]:
            self._retrace_run(26, ["/usr/bin/mock", "--configdir", task.get_savedir(), "shell", "--",
                             "bash -c 'for PKG in /packages/*; "
                             "do rpm2cpio $PKG | cpio -muid --quiet; done'"])
        self._retrace_run(27, ["/usr/bin/mock", "--configdir", task.get_savedir(), "shell",
                         "--", "chgrp -R mockbuild /var/spool/abrt/crash"])

        # generate backtrace
        task.set_status(STATUS_BACKTRACE)
        log_info(STATUS[STATUS_BACKTRACE])

        try:
            backtrace, exploitable = run_gdb(task.get_savedir())
        except Exception as ex:
            log_error(str(ex))
            self._fail()

        task.set_backtrace(backtrace)
        if exploitable is not None:
            task.add_misc("exploitable", exploitable)

        self.hook_post_retrace()

        # does not work at the moment
        rootsize = 0

        if not task.get_type() in [TASK_DEBUG, TASK_RETRACE_INTERACTIVE]:
            # clean up temporary data
            task.set_status(STATUS_CLEANUP)
            log_info(STATUS[STATUS_CLEANUP])

            self.clean_task()
            if CONFIG["UseFafPackages"]:
                shutil.rmtree(fafrepo)

            # ignore error: workdir = savedir => workdir is not empty
            if CONFIG["UseWorkDir"]:
                try:
                    os.rmdir(workdir)
                except:
                    pass

        # save crash statistics
        task.set_status(STATUS_STATS)
        log_info(STATUS[STATUS_STATS])

        task.set_finished_time(int(time.time()))
        self.stats["duration"] = int(time.time()) - self.stats["starttime"]
        self.stats["status"] = STATUS_SUCCESS

        try:
            con = init_crashstats_db()
            statsid = save_crashstats(self.stats, con)
            save_crashstats_success(statsid, self.prerunning, len(get_active_tasks()), rootsize, con)
            save_crashstats_packages(statsid, packages[1:], con)
            if missing:
                save_crashstats_build_ids(statsid, missing, con)
            con.close()
        except Exception as ex:
            log_warn(str(ex))

        # publish log => finish task
        log_info("Retrace took %d seconds" % self.stats["duration"])

        log_info(STATUS[STATUS_SUCCESS])
        task.set_status(STATUS_SUCCESS)

        self.hook_success()

        return True

    def _mock_find_vmlinux(cfgdir, candidates):
        with open(os.devnull, "w") as null:
            for cand in candidates:
                child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                               "test -f %s && echo %s" % (cand, cand)], stdout=PIPE, stderr=null)
                output = child.communicate()[0].strip()
                child.wait()
                if output == cand:
                    return cand

        return None

    def start_vmcore(self, custom_kernelver=None):
        self.hook_start()

        task = self.task

        vmcore = os.path.join(task.get_savedir(), "crash", "vmcore")

        if custom_kernelver is not None:
            kernelver = custom_kernelver
            kernelver_str = custom_kernelver.kernelver_str
        else:
            kernelver = get_kernel_release(vmcore, task.get_crash_cmd().split())
            if not kernelver:
                log_error("Unable to determine kernel version")
                self._fail()

            log_debug("Determined kernel version: %s" % kernelver)

        kernelver_str = kernelver.kernelver_str

        task.set_kernelver(str(kernelver))

        self.stats["package"] = "kernel"
        self.stats["version"] = "%s-%s" % (kernelver.version, kernelver.release)
        self.stats["arch"] = kernelver.arch

        kernelcache = os.path.join(CONFIG["RepoDir"], "kernel")
        kerneltmp = os.path.join(kernelcache, "%s.tmp" % kernelver)

        log_info(STATUS[STATUS_INIT])
        task.set_status(STATUS_INIT)
        vmlinux = ""

        if task.use_mock(kernelver):
            self.hook_post_prepare_mock()

            # we don't save config into task.get_savedir() because it is only
            # readable by user/group retrace/CONFIG["AuthGroup"].
            # if a non-retrace user in group mock executes
            # setgid /usr/bin/mock, he gets permission denied.
            # this is not a security thing - using mock gives you root anyway
            cfgdir = os.path.join(CONFIG["SaveDir"], "%d-kernel" % task.get_taskid())

            # if the directory exists, it is orphaned - nuke it
            if os.path.isdir(cfgdir):
                shutil.rmtree(cfgdir)

            mockgid = grp.getgrnam("mock").gr_gid
            old_umask = os.umask(0027)
            os.mkdir(cfgdir)
            os.chown(cfgdir, -1, mockgid)

            try:
                cfgfile = os.path.join(cfgdir, RetraceTask.MOCK_DEFAULT_CFG)
                with open(cfgfile, "w") as mockcfg:
                    mockcfg.write("config_opts['root'] = '%d-kernel'\n" % task.get_taskid())
                    mockcfg.write("config_opts['target_arch'] = '%s'\n" % kernelver.arch)
                    mockcfg.write("config_opts['chroot_setup_cmd'] = 'install bash coreutils cpio crash findutils rpm shadow-utils'\n")
                    mockcfg.write("config_opts['plugin_conf']['ccache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['yum_cache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['root_cache_enable'] = False\n")
                    mockcfg.write("config_opts['plugin_conf']['bind_mount_enable'] = True\n")
                    mockcfg.write("config_opts['plugin_conf']['bind_mount_opts'] = { \n")
                    mockcfg.write("    'dirs': [('%s', '%s'),\n" % (CONFIG["RepoDir"], CONFIG["RepoDir"]))
                    mockcfg.write("             ('%s', '%s'),],\n" % (task.get_savedir(), task.get_savedir()))
                    mockcfg.write("    'create_dirs': True, }\n")
                    mockcfg.write("\n")
                    mockcfg.write("config_opts['yum.conf'] = \"\"\"\n")
                    mockcfg.write("[main]\n")
                    mockcfg.write("cachedir=/var/cache/yum\n")
                    mockcfg.write("debuglevel=1\n")
                    mockcfg.write("reposdir=%s\n" % os.devnull)
                    mockcfg.write("logfile=/var/log/yum.log\n")
                    mockcfg.write("retries=20\n")
                    mockcfg.write("obsoletes=1\n")
                    mockcfg.write("assumeyes=1\n")
                    mockcfg.write("syslog_ident=mock\n")
                    mockcfg.write("syslog_device=\n")
                    mockcfg.write("\n")
                    mockcfg.write("#repos\n")
                    mockcfg.write("\n")
                    mockcfg.write("[kernel-%s]\n" % kernelver.arch)
                    mockcfg.write("name=kernel-%s\n" % kernelver.arch)
                    mockcfg.write("baseurl=%s\n" % CONFIG["KernelChrootRepo"].replace("$ARCH", kernelver.arch))
                    mockcfg.write("failovermethod=priority\n")
                    mockcfg.write("\"\"\"\n")

                os.chown(cfgfile, -1, mockgid)

                # symlink defaults from /etc/mock
                os.symlink("/etc/mock/site-defaults.cfg",
                           os.path.join(task.get_savedir(), RetraceTask.MOCK_SITE_DEFAULTS_CFG))
                os.symlink("/etc/mock/logging.ini",
                           os.path.join(task.get_savedir(), RetraceTask.MOCK_LOGGING_INI))
            except Exception as ex:
                log_error("Unable to create mock config file: %s" % ex)
                self._fail()
            finally:
                os.umask(old_umask)

            child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "init"], stdout=PIPE, stderr=STDOUT)
            stdout = child.communicate()[0]
            if child.wait():
                log_error("mock exitted with %d:\n%s" % (child.returncode, stdout))
                self._fail()

            self.hook_post_prepare_mock()

            # no locks required, mock locks itself
            try:
                self.hook_pre_prepare_debuginfo()
                vmlinux = prepare_debuginfo(vmcore, cfgdir, kernelver=kernelver, crash_cmd=task.get_crash_cmd().split())
                self.hook_post_prepare_debuginfo()

                self.hook_pre_retrace()
                # generate the log
                with open(os.devnull, "w") as null:
                    child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                   "crash --minimal -s %s %s" % (vmcore, vmlinux)],
                                  stdin=PIPE, stdout=PIPE, stderr=null)
                    kernellog = child.communicate("log\nquit\n")[0]
                    if child.wait():
                        log_warn("crash 'log' exitted with %d" % child.returncode)

                    child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                   "crash -s %s %s" % (vmcore, vmlinux)], stdin=PIPE, stdout=PIPE, stderr=null)
                    crash_bt_a = child.communicate("bt -a\nquit\n")[0]
                    if child.wait():
                        log_warn("crash 'bt -a' exitted with %d" % child.returncode)
                        crash_bt_a = None

                    crash_kmem_f = None
                    if CONFIG["VmcoreRunKmem"] == 1:
                        child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                       "crash -s %s %s" % (vmcore, vmlinux)], stdin=PIPE, stdout=PIPE, stderr=null)
                        crash_kmem_f = child.communicate("kmem -f\nquit\n")[0]
                        if child.wait():
                            log_warn("crash 'kmem -f' exitted with %d" % child.returncode)
                            crash_kmem_f = None

                    if CONFIG["VmcoreRunKmem"] == 2:
                        child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                       "crash -s %s %s" % (vmcore, vmlinux)], stdin=PIPE, stdout=PIPE, stderr=null)
                        crash_kmem_f = child.communicate("set hash off\nkmem -f\nset hash on\nquit\n")[0]
                        if child.wait():
                            log_warn("crash 'kmem -f' exitted with %d" % child.returncode)
                            crash_kmem_f = None

                    crash_kmem_z = None
                    if CONFIG["VmcoreRunKmem"] == 3:
                        child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                       "crash -s %s %s" % (vmcore, vmlinux)], stdin=PIPE, stdout=PIPE, stderr=null)
                        crash_kmem_z = child.communicate("kmem -z\nquit\n")[0]
                        if child.wait():
                            log_warn("crash 'kmem -z' exitted with %d" % child.returncode)
                            crash_kmem_z = None

                    child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                   "crash -s %s %s" % (vmcore, vmlinux)], stdin=PIPE, stdout=PIPE, stderr=null)
                    crash_sys = child.communicate("sys\nquit\n")[0]
                    if child.wait():
                        log_warn("crash 'sys' exitted with %d" % child.returncode)
                        crash_sys = None

                    child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                   "crash -s %s %s" % (vmcore, vmlinux)], stdin=PIPE, stdout=PIPE, stderr=null)
                    crash_sys_c = child.communicate("sys -c\nquit\n")[0]
                    if child.wait():
                        log_warn("crash 'sys -c' exitted with %d" % child.returncode)
                        crash_sys_c = None

                    child = Popen(["/usr/bin/mock", "--configdir", cfgdir, "shell", "--",
                                   "crash -s %s %s" % (vmcore, vmlinux)], stdin=PIPE, stdout=PIPE, stderr=null)
                    crash_foreach_bt = child.communicate("foreach bt\nquit\n")[0]
                    if child.wait():
                        log_warn("crash 'foreach bt' exitted with %d" % child.returncode)
                        crash_foreach_bt = None

            except Exception as ex:
                log_error(str(ex))
                self._fail()

        else:
            try:
                self.hook_pre_prepare_debuginfo()
                crash_cmd = task.get_crash_cmd().split()
                vmlinux = prepare_debuginfo(vmcore, kernelver=kernelver, crash_cmd=crash_cmd)
                task.set_crash_cmd(' '.join(crash_cmd))
                self.hook_post_prepare_debuginfo()
            except Exception as ex:
                log_error("prepare_debuginfo failed: %s" % str(ex))
                self._fail()

            self.hook_pre_retrace()
            task.set_status(STATUS_BACKTRACE)
            log_info(STATUS[STATUS_BACKTRACE])

            child = Popen(task.get_crash_cmd().split() + ["--minimal", "-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
            kernellog = child.communicate("log\nquit\n")[0]
            if child.wait():
                log_warn("crash 'log' exited with %d" % child.returncode)

            child = Popen(task.get_crash_cmd().split() + ["-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
            crash_bt_a = child.communicate("bt -a\nquit\n")[0]
            if child.wait():
                log_warn("crash 'bt -a' exited with %d" % child.returncode)
                crash_bt_a = None

            crash_kmem_f = None
            if CONFIG["VmcoreRunKmem"] == 1:
                child = Popen(task.get_crash_cmd().split() + ["-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
                crash_kmem_f = child.communicate("kmem -f\nquit\n")[0]
                if child.wait():
                    log_warn("crash 'kmem -f' exited with %d" % child.returncode)
                    crash_kmem_f = None

            if CONFIG["VmcoreRunKmem"] == 2:
                child = Popen(task.get_crash_cmd().split() + ["-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
                crash_kmem_f = child.communicate("set hash off\nkmem -f\nset hash on\nquit\n")[0]
                if child.wait():
                    log_warn("crash 'kmem -f' exited with %d" % child.returncode)
                    crash_kmem_f = None

            crash_kmem_z = None
            if CONFIG["VmcoreRunKmem"] == 3:
                child = Popen(task.get_crash_cmd().split() + ["-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
                crash_kmem_z = child.communicate("kmem -z\nquit\n")[0]
                if child.wait():
                    log_warn("crash 'kmem -z' exited with %d" % child.returncode)
                    crash_kmem_z = None

            child = Popen(task.get_crash_cmd().split() +  ["-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
            crash_sys = child.communicate("sys\nquit\n")[0]
            if child.wait():
                log_warn("crash 'sys' exited with %d" % child.returncode)
                crash_sys = None

            child = Popen(task.get_crash_cmd().split() + ["-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
            crash_sys_c = child.communicate("sys -c\nquit\n")[0]
            if child.wait():
                log_warn("crash 'sys -c' exited with %d" % child.returncode)
                crash_sys_c = None

            child = Popen(task.get_crash_cmd().split() + ["-s", vmcore, vmlinux], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
            crash_foreach_bt = child.communicate("foreach bt\nquit\n")[0]
            if child.wait():
                log_warn("crash 'foreach bt' exited with %d" % child.returncode)
                crash_foreach_bt = None

        task.set_backtrace(kernellog)
        if crash_bt_a:
            task.add_misc("bt-a", crash_bt_a)
        if crash_kmem_f:
            task.add_misc("kmem-f", crash_kmem_f)
        if crash_kmem_z:
            task.add_misc("kmem-z", crash_kmem_z)
        if crash_sys:
            task.add_misc("sys", crash_sys)
        if crash_sys_c:
            task.add_misc("sys-c", crash_sys_c)
        if crash_foreach_bt:
            child = Popen(["bt_filter"], stdin=PIPE, stdout=PIPE, stderr=STDOUT)
            bt_filter = child.communicate(crash_foreach_bt)[0]
            if child.wait():
                bt_filter = "bt_filter exitted with %d\n\n%s" % (child.returncode, bt_filter)

            task.add_misc("bt-filter", bt_filter)

        crashrc_lines = []

        if "/" in vmlinux:
            crashrc_lines.append("mod -S %s > %s" % (vmlinux.rsplit("/", 1)[0], os.devnull))

        miscdir = os.path.join(task.get_savedir(), RetraceTask.MISC_DIR)
        crashrc_lines.append("cd %s" % miscdir)

        if len(crashrc_lines) > 0:
            task.set_crashrc("%s\n" % "\n".join(crashrc_lines))

        self.hook_post_retrace()

        task.set_finished_time(int(time.time()))
        self.stats["duration"] = int(time.time()) - self.stats["starttime"]
        self.stats["status"] = STATUS_SUCCESS

        log_info(STATUS[STATUS_STATS])

        try:
            save_crashstats(self.stats)
        except Exception as ex:
            log_error(str(ex))

        # clean up temporary data
        task.set_status(STATUS_CLEANUP)
        log_info(STATUS[STATUS_CLEANUP])

        if not task.get_type() in [TASK_VMCORE_INTERACTIVE]:
            self.clean_task()

        if CONFIG["EmailNotify"] and task.has_notify():
            try:
                log_info("Sending e-mail to %s" % ", ".join(task.get_notify()))

                message = "The task #%d started on %s succeeded\n\n" % (task.get_taskid(), os.uname()[1])

                if task.has_url():
                    message += "URL: %s\n" % task.get_url()

                message += "Task directory: %s\n" % task.get_savedir()

                if task.has_started_time():
                    message += "Started: %s\n" % datetime.datetime.fromtimestamp(task.get_started_time())

                if task.has_finished_time():
                    message += "Finished: %s\n" % datetime.datetime.fromtimestamp(task.get_finished_time())

                if task.has_remote() or task.has_downloaded():
                    files = ""
                    if task.has_remote():
                        remote = map(lambda x: x[4:] if x.startswith("FTP ") else x, task.get_remote())
                        files = ", ".join(remote)

                    if task.has_downloaded():
                        files = ", ".join(filter(None, [task.get_downloaded(), files]))

                    message += "Remote file(s): %s\n" % files

                if taks.has_log():
                    message += "\nLog:\n%s\n" % task.get_log()

                send_email("Retrace Server <%s>" % CONFIG["EmailNotifyFrom"],
                           task.get_notify(),
                           "Retrace Task #%d on %s succeded" % (task.get_taskid(), os.uname()[1]),
                           message)

            except Exception as ex:
                log_error("Failed to send e-mail: %s" % ex)

        log_info("Retrace took %d seconds" % self.stats["duration"])
        log_info(STATUS[STATUS_SUCCESS])

        if task.has_log():
            # add a symlink to log to misc directory
            # use underscore so that the log is first in the list
            os.symlink(task._get_file_path(RetraceTask.LOG_FILE),
                       os.path.join(task._get_file_path(RetraceTask.MISC_DIR), "retrace-log"))

        task.set_status(STATUS_SUCCESS)
        self.hook_success()

    def start(self, kernelver=None, arch=None):
        self.hook_pre_start()
        self.stats = {
            "taskid": self.task.get_taskid(),
            "package": None,
            "version": None,
            "arch": None,
            "starttime": int(time.time()),
            "duration": None,
            "coresize": None,
            "status": STATUS_FAIL,
        }
        self.prerunning = len(get_active_tasks()) - 1
        try:
            task = self.task

            task.set_started_time(int(time.time()))

            if task.has_remote():
                errors = task.download_remote(kernelver=kernelver)
                if errors:
                    for error in errors:
                        log_warn(error)

            task.set_status(STATUS_ANALYZE)
            log_info(STATUS[STATUS_ANALYZE])

            crashdir = os.path.join(task.get_savedir(), "crash")

            tasktype = task.get_type()

            if task.has("custom_executable"):
                shutil.copyfile(task._get_file_path("custom_executable"),
                                os.path.join(crashdir, "executable"))
            if task.has("custom_package"):
                shutil.copyfile(task._get_file_path("custom_package"),
                                os.path.join(crashdir, "package"))
            if task.has("custom_os_release"):
                shutil.copyfile(task._get_file_path("custom_os_release"),
                                os.path.join(crashdir, "os_release"))

            for required_file in REQUIRED_FILES[tasktype]:
                if not os.path.isfile(os.path.join(crashdir, required_file)):
                    log_error("Crash directory does not contain required file '%s'" % required_file)
                    self._fail()

            if tasktype in [TASK_RETRACE, TASK_DEBUG, TASK_RETRACE_INTERACTIVE]:
                self.start_retrace(custom_arch=arch)
            elif tasktype in [TASK_VMCORE, TASK_VMCORE_INTERACTIVE]:
                self.start_vmcore(custom_kernelver=kernelver)
            else:
                raise Exception("Unsupported task type")
        except Exception as ex:
            log_error(str(ex))
            self._fail()

    def clean_task(self):
        self.hook_pre_clean_task()
        ret = self.task.clean()
        self.hook_post_clean_task()
        return ret

    def remove_task(self):
        self.hook_pre_remove_task()
        ret = self.task.remove()
        self.hook_post_remove_task()
        return ret
