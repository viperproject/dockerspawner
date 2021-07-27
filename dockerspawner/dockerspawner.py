"""
A Spawner for JupyterHub that runs each user's server in a separate docker container
"""
import asyncio
import os
import string
import warnings
import tarfile
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from io import BytesIO
from pprint import pformat
from tarfile import TarFile
from tarfile import TarInfo
from textwrap import dedent
from textwrap import indent
from urllib.parse import urlparse

import docker
from docker.errors import APIError
from docker.types import Mount
from docker.utils import kwargs_from_env
from escapism import escape
from jupyterhub.spawner import Spawner
from tornado import web
from traitlets import Any
from traitlets import Bool
from traitlets import CaselessStrEnum
from traitlets import default
from traitlets import Dict
from traitlets import Int
from traitlets import List
from traitlets import observe
from traitlets import Unicode
from traitlets import Union
from traitlets import validate

from .volumenamingstrategy import default_format_volume_name


class UnicodeOrFalse(Unicode):
    info_text = "a unicode string or False"

    def validate(self, obj, value):
        if value is False:
            return value

        return super(UnicodeOrFalse, self).validate(obj, value)


import jupyterhub

_jupyterhub_xy = "%i.%i" % (jupyterhub.version_info[:2])


class DockerSpawner(Spawner):
    """A Spawner for JupyterHub that runs each user's server in a separate docker container"""

    _executor = None

    @property
    def executor(self):
        """single global executor"""
        cls = self.__class__
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(1)
        return cls._executor

    _client = None

    @property
    def client(self):
        """single global client instance"""
        cls = self.__class__
        if cls._client is None:
            kwargs = {"version": "auto"}
            if self.tls_config:
                kwargs["tls"] = docker.tls.TLSConfig(**self.tls_config)
            kwargs.update(kwargs_from_env())
            kwargs.update(self.client_kwargs)
            client = docker.APIClient(**kwargs)
            cls._client = client
        return cls._client

    @default("cmd")
    def _default_cmd(self):
        # no default means use the image command
        return None

    object_id = Unicode()
    # the type of object we create
    object_type = "container"
    # the field containing the object id
    object_id_key = "Id"

    @property
    def container_id(self):
        """alias for object_id"""
        return self.object_id

    @property
    def container_name(self):
        """alias for object_name"""
        return self.object_name

    host_ip = Unicode(
        "127.0.0.1",
        help="""The ip address on the host on which to expose the container's port

        Typically 127.0.0.1, but can be public interfaces as well
        in cases where the Hub and/or proxy are on different machines
        from the user containers.

        Only used when use_internal_ip = False.
        """,
        config=True,
    )

    @default('host_ip')
    def _default_host_ip(self):
        docker_host = os.getenv('DOCKER_HOST')
        if docker_host:
            urlinfo = urlparse(docker_host)
            if urlinfo.scheme == 'tcp':
                return urlinfo.hostname
        return '127.0.0.1'


    # fix default port to 8080, used in the container

    @default("port")
    def _port_default(self):
        return 8080

    # default to listening on all-interfaces in the container

    @default("ip")
    def _ip_default(self):
        return "0.0.0.0"


    image = Unicode(
        "jupyterhub/singleuser:%s" % _jupyterhub_xy,
        config=True,
        help="""The image to use for single-user servers.

        This image should have the same version of jupyterhub as
        the Hub itself installed.

        If the default command of the image does not launch
        jupyterhub-singleuser, set `c.Spawner.cmd` to
        launch jupyterhub-singleuser, e.g.

        Any of the jupyter docker-stacks should work without additional config,
        as long as the version of jupyterhub in the image is compatible.
        """,
    )

    allowed_images = Union(
        [Any(), Dict(), List()],
        default_value={},
        config=True,
        help="""
        List or dict of images that users can run.

        If specified, users will be presented with a form
        from which they can select an image to run.

        If a dictionary, the keys will be the options presented to users
        and the values the actual images that will be launched.

        If a list, will be cast to a dictionary where keys and values are the same
        (i.e. a shortcut for presenting the actual images directly to users).

        If a callable, will be called with the Spawner instance as its only argument.
        The user is accessible as spawner.user.
        The callable should return a dict or list as above.

        .. versionchanged:: 12.0
            `DockerSpawner.image_whitelist` renamed to `allowed_images`

        """,
    )

    @validate('allowed_images')
    def _allowed_images_dict(self, proposal):
        """cast allowed_images to a dict

        If passing a list, cast it to a {item:item}
        dict where the keys and values are the same.
        """
        allowed_images = proposal.value
        if isinstance(allowed_images, list):
            allowed_images = {item: item for item in allowed_images}
        return allowed_images

    def _get_allowed_images(self):
        """Evaluate allowed_images callable

        Or return the list as-is if it's already a dict
        """
        if callable(self.allowed_images):
            allowed_images = self.allowed_images(self)
            if not isinstance(allowed_images, dict):
                # always return a dict
                allowed_images = {item: item for item in allowed_images}
            return allowed_images
        return self.allowed_images

    @default('options_form')
    def _default_options_form(self):
        allowed_images = self._get_allowed_images()
        if len(allowed_images) <= 1:
            # default form only when there are images to choose from
            return ''
        # form derived from wrapspawner.ProfileSpawner
        option_t = '<option value="{image}" {selected}>{image}</option>'
        options = [
            option_t.format(
                image=image, selected='selected' if image == self.image else ''
            )
            for image in allowed_images
        ]
        return """
        <label for="image">Select an image:</label>
        <select class="form-control" name="image" required autofocus>
        {options}
        </select>
        """.format(
            options=options
        )

    def options_from_form(self, formdata):
        """Turn options formdata into user_options"""
        options = {}
        if 'image' in formdata:
            options['image'] = formdata['image'][0]
        return options

    pull_policy = CaselessStrEnum(
        ["always", "ifnotpresent", "never", "skip"],
        default_value="ifnotpresent",
        config=True,
        help="""The policy for pulling the user docker image.

        Choices:

        - ifnotpresent: pull if the image is not already present (default)
        - always: always pull the image to check for updates,
          even if it is present
        - never: never perform a pull, raise if image is not present
        - skip: never perform a pull, skip the step entirely
          (like never, but without raising when images are not present;
          default for swarm)

        .. versionadded: 12.0
            'skip' option added. It is the default for swarm
            because pre-pulling images on swarm clusters
            doesn't make sense since the container is likely not
            going to run on the same node where the image was pulled.
        """,
    )


    prefix = Unicode(
        "jupyter",
        config=True,
        help=dedent(
            """
            Prefix for container names. See name_template for full container name for a particular
            user's server.
            """
        ),
    )

    name_template = Unicode(
        config=True,
        help=dedent(
            """
            Name of the container or service: with {username}, {imagename}, {prefix}, {servername} replacements.
            {raw_username} can be used for the original, not escaped username
            (may contain uppercase, special characters).
            It is important to include {servername} if JupyterHub's "named
            servers" are enabled (JupyterHub.allow_named_servers = True).
            If the server is named, the default name_template is
            "{prefix}-{username}--{servername}". If it is unnamed, the default
            name_template is "{prefix}-{username}".

            Note: when using named servers,
            it is important that the separator between {username} and {servername}
            is not a character that can occur in an escaped {username},
            and also not the single escape character '-'.
            """
        ),
    )

    @default('name_template')
    def _default_name_template(self):
        if self.name:
            return "{prefix}-{username}--{servername}"
        else:
            return "{prefix}-{username}"

    client_kwargs = Dict(
        config=True,
        help="Extra keyword arguments to pass to the docker.Client constructor.",
    )

    volumes = Dict(
        config=True,
        help=dedent(
            """
            Map from host file/directory to container (guest) file/directory
            mount point and (optionally) a mode. When specifying the
            guest mount point (bind) for the volume, you may use a
            dict or str. If a str, then the volume will default to a
            read-write (mode="rw"). With a dict, the bind is
            identified by "bind" and the "mode" may be one of "rw"
            (default), "ro" (read-only), "z" (public/shared SELinux
            volume label), and "Z" (private/unshared SELinux volume
            label).

            If format_volume_name is not set,
            default_format_volume_name is used for naming volumes.
            In this case, if you use {username} in either the host or guest
            file/directory path, it will be replaced with the current
            user's name.
            """
        ),
    )

    mounts = List(
        config=True,
        help=dedent(
            """
            List of dict with keys to match docker.types.Mount for more advanced 
            configuration of mouted volumes.  As with volumes, if the default
            format_volume_name is in use, you can use {username} in the source or 
            target paths, and it will be replaced with the current user's name.
            """
        ),
    )

    move_certs_image = Unicode(
        "busybox:1.30.1",
        config=True,
        help="""The image used to stage internal SSL certificates.

        Busybox is used because we just need an empty container
        that waits while we stage files into the volume via .put_archive.
        """,
    )

    async def move_certs(self, paths):
        self.log.info("Staging internal ssl certs for %s", self._log_name)
        await self.pull_image(self.move_certs_image)
        # create the volume
        volume_name = self.format_volume_name(self.certs_volume_name, self)
        # create volume passes even if it already exists
        self.log.info("Creating ssl volume %s for %s", volume_name, self._log_name)
        await self.docker('create_volume', volume_name)

        # create a tar archive of the internal cert files
        # docker.put_archive takes a tarfile and a running container
        # and unpacks the archive into the container
        nb_paths = {}
        tar_buf = BytesIO()
        archive = TarFile(fileobj=tar_buf, mode='w')
        for key, hub_path in paths.items():
            fname = os.path.basename(hub_path)
            nb_paths[key] = '/certs/' + fname
            with open(hub_path, 'rb') as f:
                content = f.read()
            tarinfo = TarInfo(name=fname)
            tarinfo.size = len(content)
            tarinfo.mtime = os.stat(hub_path).st_mtime
            tarinfo.mode = 0o644
            archive.addfile(tarinfo, BytesIO(content))
        archive.close()
        tar_buf.seek(0)

        # run a container to stage the certs,
        # mounting the volume at /certs/
        host_config = self.client.create_host_config(
            binds={
                volume_name: {"bind": "/certs", "mode": "rw"},
            },
        )
        container = await self.docker(
            'create_container',
            self.move_certs_image,
            volumes=["/certs"],
            host_config=host_config,
        )

        container_id = container['Id']
        self.log.debug(
            "Container %s is creating ssl certs for %s",
            container_id[:12],
            self._log_name,
        )
        # start the container
        await self.docker('start', container_id)
        # stage the archive to the container
        try:
            await self.docker(
                'put_archive',
                container=container_id,
                path='/certs',
                data=tar_buf,
            )
        finally:
            await self.docker('remove_container', container_id)
        return nb_paths

    certs_volume_name = Unicode(
        "{prefix}ssl-{username}",
        config=True,
        help="""Volume name

        The same string-templating applies to this
        as other volume names.
        """,
    )

    read_only_volumes = Dict(
        config=True,
        help=dedent(
            """
            Map from host file/directory to container file/directory.
            Volumes specified here will be read-only in the container.

            If format_volume_name is not set,
            default_format_volume_name is used for naming volumes.
            In this case, if you use {username} in either the host or guest
            file/directory path, it will be replaced with the current
            user's name.
            """
        ),
    )

    format_volume_name = Any(
        help="""Any callable that accepts a string template and a DockerSpawner instance as parameters in that order and returns a string.

        Reusable implementations should go in dockerspawner.VolumeNamingStrategy, tests should go in ...
        """
    ).tag(config=True)

    @default("format_volume_name")
    def _get_default_format_volume_name(self):
        return default_format_volume_name


    tls_config = Dict(
        config=True,
        help="""Arguments to pass to docker TLS configuration.

        See docker.client.TLSConfig constructor for options.
        """,
    )

    @observe(
        "tls", "tls_verify", "tls_ca", "tls_cert", "tls_key", "tls_assert_hostname"
    )
    def _tls_changed(self, change):
        self.log.warning(
            "%s config ignored, use %s.tls_config dict to set full TLS configuration.",
            change.name,
            self.__class__.__name__,
        )


    remove = Bool(
        False,
        config=True,
        help="""
        If True, delete containers when servers are stopped.

        This will destroy any data in the container not stored in mounted volumes.
        """,
    )

    @property
    def will_resume(self):
        # indicate that we will resume,
        # so JupyterHub >= 0.7.1 won't cleanup our API token
        return not self.remove

    extra_create_kwargs = Dict(
        config=True, help="Additional args to pass for container create"
    )
    extra_host_config = Dict(
        config=True, help="Additional args to create_host_config for container create"
    )

    escape = Any(
        help="""Override escaping with any callable of the form escape(str)->str

        This is used to ensure docker-safe container names, etc.

        The default escaping should ensure safety and validity,
        but can produce cumbersome strings in cases.

        Set c.DockerSpawner.escape = 'legacy' to preserve the earlier, unsafe behavior
        if it worked for you.

        .. versionadded:: 12.0

        .. versionchanged:: 12.0
            Escaping has changed in 12.0 to ensure safety,
            but existing deployments will get different container and volume names.
        """,
        config=True,
    )

    @default("escape")
    def _escape_default(self):
        return self._escape

    @validate("escape")
    def _validate_escape(self, proposal):
        escape = proposal.value
        if escape == "legacy":
            return self._legacy_escape
        if not callable(escape):
            raise ValueError("DockerSpawner.escape must be callable, got %r" % escape)
        return escape

    @staticmethod
    def _escape(text):
        # Make sure a substring matches the restrictions for DNS labels
        # Note: '-' cannot be in safe_chars, as it is being used as escape character
        # any '-' must be escaped to '-2d' to avoid collisions
        safe_chars = set(string.ascii_lowercase + string.digits)
        return escape(text, safe_chars, escape_char='-').lower()

    @staticmethod
    def _legacy_escape(text):
        """Legacy implementation of escape

        Select with config c.DockerSpawner.escape = 'legacy'

        Unsafe and doesn't work in all cases,
        but allows opt-in to backward compatibility for an upgrading deployment.

        Do not use for new deployments.
        """
        safe_chars = set(string.ascii_letters + string.digits + "-")
        return escape(text, safe_chars, escape_char='_')

    hub_ip_connect = Unicode(
        config=True,
        help=dedent(
            """
            If set, DockerSpawner will configure the containers to use
            the specified IP to connect the hub api.  This is useful
            when the hub_api is bound to listen on all ports or is
            running inside of a container.
            """
        ),
    )

    @observe("hub_ip_connect")
    def _ip_connect_changed(self, change):
        if jupyterhub.version_info >= (0, 8):
            warnings.warn(
                "DockerSpawner.hub_ip_connect is no longer needed with JupyterHub 0.8."
                "  Use JupyterHub.hub_connect_ip instead.",
                DeprecationWarning,
            )

    use_internal_ip = Bool(
        False,
        config=True,
        help=dedent(
            """
            Enable the usage of the internal docker ip. This is useful if you are running
            jupyterhub (as a container) and the user containers within the same docker network.
            E.g. by mounting the docker socket of the host into the jupyterhub container.
            Default is True if using a docker network, False if bridge or host networking is used.
            """
        ),
    )

    @default("use_internal_ip")
    def _default_use_ip(self):
        # setting network_name to something other than bridge or host implies use_internal_ip
        if self.network_name not in {"bridge", "host"}:
            return True

        else:
            return False

    use_internal_hostname = Bool(
        False,
        config=True,
        help=dedent(
            """
            Use the docker hostname for connecting.

            instead of an IP address.
            This should work in general when using docker networks,
            and must be used when internal_ssl is enabled.
            It is enabled by default if internal_ssl is enabled.
            """
        ),
    )

    @default("use_internal_hostname")
    def _default_use_hostname(self):
        # FIXME: replace getattr with self.internal_ssl
        # when minimum jupyterhub is 1.0
        return getattr(self, 'internal_ssl', False)

    links = Dict(
        config=True,
        help=dedent(
            """
            Specify docker link mapping to add to the container, e.g.

                links = {'jupyterhub': 'jupyterhub'}

            If the Hub is running in a Docker container,
            this can simplify routing because all traffic will be using docker hostnames.
            """
        ),
    )

    network_name = Unicode(
        "bridge",
        config=True,
        help=dedent(
            """
            Run the containers on this docker network.
            If it is an internal docker network, the Hub should be on the same network,
            as internal docker IP addresses will be used.
            For bridge networking, external ports will be bound.
            """
        ),
    )

    post_start_cmd = UnicodeOrFalse(
        False,
        config=True,
        help="""If specified, the command will be executed inside the container
        after starting.
        Similar to using 'docker exec'
        """,
    )

    async def post_start_exec(self):
        """
        Execute additional command inside the container after starting it.

        e.g. calling 'docker exec'
        """

        container = await self.get_object()
        container_id = container[self.object_id_key]

        exec_kwargs = {'cmd': self.post_start_cmd, 'container': container_id}
        self.log.debug(
            f"Running post_start exec in {self.object_name}: {self.post_start_cmd}"
        )

        exec_id = await self.docker("exec_create", **exec_kwargs)

        stdout, stderr = await self.docker("exec_start", exec_id=exec_id, demux=True)

        # docker-py uses None for empty output instead of empty bytestring
        if stdout is None:
            stdout = b''

        # stderr is usually None instead of empty b''
        # this includes error conditions like "OCI runtime exec failed..."
        # but also most successful runs
        if stderr is None:
            # crude check for "OCI runtime exec failed: ..."
            # switch message to stderr instead of stdout for warning-level output
            if b'exec failed' in stdout:
                stderr = stdout
                stdout = b''
            else:
                stderr = b''

        for name, stream, level in [
            ("stdout", stdout, "debug"),
            ("stderr", stderr, "warning"),
        ]:
            output = stream.decode("utf8", "replace").strip()
            if not output:
                continue

            if '\n' in output:
                # if multi-line, wrap to new line and indent
                output = '\n' + output
                output = indent(output, "    ")
            log = getattr(self.log, level)
            log(f"post_start {name} in {self.object_name}: {output}")

    @property
    def tls_client(self):
        """A tuple consisting of the TLS client certificate and key if they
        have been provided, otherwise None.

        """
        if self.tls_cert and self.tls_key:
            return (self.tls_cert, self.tls_key)

        return None

    @property
    def volume_mount_points(self):
        """
        Volumes are declared in docker-py in two stages.  First, you declare
        all the locations where you're going to mount volumes when you call
        create_container.

        Returns a sorted list of all the values in self.volumes or
        self.read_only_volumes.
        """
        return sorted([value["bind"] for value in self.volume_binds.values()])

    @property
    def volume_binds(self):
        """
        The second half of declaring a volume with docker-py happens when you
        actually call start(). The required format is a dict of dicts that
        looks like::

            {
                host_location: {'bind': container_location, 'mode': 'rw'}
            }

        Mode may be 'ro', 'rw', 'z', or 'Z'.
        """
        binds = self._volumes_to_binds(self.volumes, {})
        read_only_volumes = {}
        # FIXME: replace getattr with self.internal_ssl
        # when minimum jupyterhub is 1.0
        if getattr(self, 'internal_ssl', False):
            # add SSL volume as read-only
            read_only_volumes[self.certs_volume_name] = '/certs'
        read_only_volumes.update(self.read_only_volumes)
        return self._volumes_to_binds(read_only_volumes, binds, mode="ro")

    @property
    def mount_binds(self):
        """
        A different way of specifying docker volumes using more advanced spec.
        Converts mounts list of dict to a list of docker.types.Mount
        """

        def _fmt(v):
            return self.format_volume_name(v, self)

        mounts = []
        for mount in self.mounts:
            args = dict(mount)
            args["source"] = _fmt(mount["source"])
            args["target"] = _fmt(mount["target"])
            mounts.append(Mount(**args))
        return mounts

    _escaped_name = None

    @property
    def escaped_name(self):
        """Escape the username so it's safe for docker objects"""
        if self._escaped_name is None:
            self._escaped_name = self.escape(self.user.name)
        return self._escaped_name

    object_id = Unicode(allow_none=True)

    def template_namespace(self):
        escaped_image = self.image.replace("/", "-")
        server_name = getattr(self, "name", "")
        safe_server_name = self.escape(server_name.lower())
        return {
            "username": self.escaped_name,
            "safe_username": self.escaped_name,
            "raw_username": self.user.name,
            "imagename": escaped_image,
            "servername": safe_server_name,
            "raw_servername": server_name,
            "prefix": self.prefix,
        }

    object_name = Unicode()

    @default("object_name")
    def _object_name_default(self):
        """Render the name of our container/service using name_template"""
        return self._render_templates(self.name_template)

    def load_state(self, state):
        super(DockerSpawner, self).load_state(state)
        if "container_id" in state:
            # backward-compatibility for dockerspawner < 0.10
            self.object_id = state.get("container_id")
        else:
            self.object_id = state.get("object_id", "")

        # override object_name from state if defined
        # to avoid losing track of running servers
        self.object_name = state.get("object_name", None) or self.object_name

    def get_state(self):
        state = super(DockerSpawner, self).get_state()
        if self.object_id:
            state["object_id"] = self.object_id
            # persist object_name if running
            # so that a change in the template doesn't lose track of running servers
            state["object_name"] = self.object_name
        return state

    def _public_hub_api_url(self):
        proto, path = self.hub.api_url.split("://", 1)
        ip, rest = path.split(":", 1)
        return "{proto}://{ip}:{rest}".format(
            proto=proto, ip=self.hub_ip_connect, rest=rest
        )

    def _env_keep_default(self):
        """Don't inherit any env from the parent process"""
        return []

    def get_args(self):
        args = super().get_args()
        if self.hub_ip_connect:
            # JupyterHub 0.7 specifies --hub-api-url
            # on the command-line, which is hard to update
            for idx, arg in enumerate(list(args)):
                if arg.startswith("--hub-api-url="):
                    args.pop(idx)
                    break

            args.append("--hub-api-url=%s" % self._public_hub_api_url())
        return args

    def get_env(self):
        env = super().get_env()
        env['JUPYTER_IMAGE_SPEC'] = self.image
        env['JUPYTER_SOCKET_NAMESPACE'] = '/socket' + self.proxy_spec
        return env

    def _docker(self, method, *args, **kwargs):
        """wrapper for calling docker methods

        to be passed to ThreadPoolExecutor
        """
        m = getattr(self.client, method)
        return m(*args, **kwargs)

    def docker(self, method, *args, **kwargs):
        """Call a docker method in a background thread

        returns a Future
        """
        return asyncio.wrap_future(
            self.executor.submit(self._docker, method, *args, **kwargs)
        )

    async def poll(self):
        """Check for my id in `docker ps`"""
        container = await self.get_object()
        if not container:
            self.log.warning("Container not found: %s", self.container_name)
            return 0

        container_state = container["State"]
        self.log.debug(
            "Container %s status: %s", self.container_id[:7], pformat(container_state)
        )

        if container_state["Running"]:
            return None

        else:
            return (
                "ExitCode={ExitCode}, "
                "Error='{Error}', "
                "FinishedAt={FinishedAt}".format(**container_state)
            )

    async def get_object(self):
        self.log.debug("Getting %s '%s'", self.object_type, self.object_name)
        try:
            obj = await self.docker("inspect_%s" % self.object_type, self.object_name)
            self.object_id = obj[self.object_id_key]
        except APIError as e:
            if e.response.status_code == 404:
                self.log.info(
                    "%s '%s' is gone", self.object_type.title(), self.object_name
                )
                obj = None
                # my container is gone, forget my id
                self.object_id = ""
            elif e.response.status_code == 500:
                self.log.info(
                    "%s '%s' is on unhealthy node",
                    self.object_type.title(),
                    self.object_name,
                )
                obj = None
                # my container is unhealthy, forget my id
                self.object_id = ""
            else:
                raise

        return obj

    async def get_command(self):
        """Get the command to run (full command + args)"""
        if self.cmd:
            cmd = self.cmd
        else:
            image_info = await self.docker("inspect_image", self.image)
            cmd = image_info["Config"]["Cmd"]
        return cmd 

    async def remove_object(self):
        self.log.info("Removing %s %s", self.object_type, self.object_id)
        # remove the container, as well as any associated volumes
        try:
            await self.docker("remove_" + self.object_type, self.object_id, v=True)
        except docker.errors.APIError as e:
            if e.status_code == 409:
                self.log.debug(
                    "Already removing %s: %s", self.object_type, self.object_id
                )
            elif e.status_code == 404:
                self.log.debug(
                    "Already removed %s: %s", self.object_type, self.object_id
                )
            else:
                raise

    async def check_allowed(self, image):
        allowed_images = self._get_allowed_images()
        if not allowed_images:
            return image
        if image not in allowed_images:
            raise web.HTTPError(
                400,
                "Image %s not in allowed list: %s" % (image, ', '.join(allowed_images)),
            )
        # resolve image alias to actual image name
        return allowed_images[image]

    @default('ssl_alt_names')
    def _get_ssl_alt_names(self):
        return ['DNS:' + self.internal_hostname]

    async def create_object(self):
        """Create the container/service object"""

        create_kwargs = dict(
            image=self.image,
            environment=self.get_env(),
            volumes=self.volume_mount_points,
            name=self.container_name,
            command=(await self.get_command()),
        )

        # ensure internal port is exposed
        create_kwargs["ports"] = {"%i/tcp" % self.port: None}

        create_kwargs.update(self._render_templates(self.extra_create_kwargs))

        # build the dictionary of keyword arguments for host_config
        host_config = dict(
            auto_remove=self.remove,
            binds=self.volume_binds,
            links=self.links,
            mounts=self.mount_binds,
        )

        if getattr(self, "mem_limit", None) is not None:
            # If jupyterhub version > 0.7, mem_limit is a traitlet that can
            # be directly configured. If so, use it to set mem_limit.
            # this will still be overriden by extra_host_config
            host_config["mem_limit"] = self.mem_limit

        if not self.use_internal_ip:
            host_config["port_bindings"] = {self.port: (self.host_ip,)}
        host_config.update(self._render_templates(self.extra_host_config))
        host_config.setdefault("network_mode", self.network_name)

        self.log.debug("Starting host with config: %s", host_config)

        host_config = self.client.create_host_config(**host_config)
        create_kwargs.setdefault("host_config", {}).update(host_config)

        # create the container
        obj = await self.docker("create_container", **create_kwargs)
        return obj

    async def start_object(self):
        """Actually start the container/service

        e.g. calling `docker start`
        """
        await self.docker("start", self.container_id)

    async def stop_object(self):
        """Stop the container/service

        e.g. calling `docker stop`. Does not remove the container.
        """
        try:
            await self.docker("stop", self.container_id)
        except APIError as e:
            if e.status_code == 404:
                self.log.debug(
                    "Already removed %s: %s", self.object_type, self.object_id
                )
                return
            else:
                raise

    async def pull_image(self, image):
        """Pull the image, if needed

        - pulls it unconditionally if pull_policy == 'always'
        - skipped entirely if pull_policy == 'skip' (default for swarm)
        - otherwise, checks if it exists, and
          - raises if pull_policy == 'never'
          - pulls if pull_policy == 'ifnotpresent'
        """
        if self.pull_policy == "skip":
            self.log.debug(f"Skipping pull of {image}")
            return
        # docker wants to split repo:tag
        # the part split("/")[-1] allows having an image from a custom repo
        # with port but without tag. For example: my.docker.repo:51150/foo would not
        # pass this test, resulting in image=my.docker.repo:51150/foo and tag=latest
        if ':' in image.split("/")[-1]:
            # rsplit splits from right to left, allowing to have a custom image repo with port
            repo, tag = image.rsplit(':', 1)
        else:
            repo = image
            tag = 'latest'

        if self.pull_policy.lower() == 'always':
            # always pull
            self.log.info("pulling %s", image)
            await self.docker('pull', repo, tag)
            # done
            return
        try:
            # check if the image is present
            await self.docker('inspect_image', image)
        except docker.errors.NotFound:
            if self.pull_policy == "never":
                # never pull, raise because there is no such image
                raise
            elif self.pull_policy == "ifnotpresent":
                # not present, pull it for the first time
                self.log.info("pulling image %s", image)
                await self.docker('pull', repo, tag)

    async def start(self, image=None, extra_create_kwargs=None, extra_host_config=None):
        """Start the single-user server in a docker container.

        Additional arguments to create/host config/etc. can be specified
        via .extra_create_kwargs and .extra_host_config attributes.

        If the container exists and `c.DockerSpawner.remove` is true, then
        the container is removed first. Otherwise, the existing containers
        will be restarted.
        """

        if image:
            self.log.warning("Specifying image via .start args is deprecated")
            self.image = image
        if extra_create_kwargs:
            self.log.warning(
                "Specifying extra_create_kwargs via .start args is deprecated"
            )
            self.extra_create_kwargs.update(extra_create_kwargs)
        if extra_host_config:
            self.log.warning(
                "Specifying extra_host_config via .start args is deprecated"
            )
            self.extra_host_config.update(extra_host_config)

        # image priority:
        # 1. user options (from spawn options form)
        # 2. self.image from config
        image_option = self.user_options.get('image')
        if image_option:
            # save choice in self.image
            self.image = await self.check_allowed(image_option)

        image = self.image
        await self.pull_image(image)

        obj = await self.get_object()
        if obj and self.remove:
            self.log.warning(
                "Removing %s that should have been cleaned up: %s (id: %s)",
                self.object_type,
                self.object_name,
                self.object_id[:7],
            )
            await self.remove_object()

            obj = None

        if obj is None:
            obj = await self.create_object()
            self.object_id = obj[self.object_id_key]
            self.log.info(
                "Created %s %s (id: %s) from image %s",
                self.object_type,
                self.object_name,
                self.object_id[:7],
                self.image,
            )

        else:
            self.log.info(
                "Found existing %s %s (id: %s)",
                self.object_type,
                self.object_name,
                self.object_id[:7],
            )
            # Handle re-using API token.
            # Get the API token from the environment variables
            # of the running container:
            for line in obj["Config"]["Env"]:
                if line.startswith(("JPY_API_TOKEN=", "JUPYTERHUB_API_TOKEN=")):
                    self.api_token = line.split("=", 1)[1]
                    break

        #add code-server config with custom password located in self.user.server_passwd
        docker_client = docker.from_env()
        container = docker_client.containers.get(self.container_id)
        with open('/srv/jupyterhub/configs/config_{}.yaml'.format(self.user.escaped_name), 'w') as f:
            f.write(f"bind-addr: 127.0.0.1:8080\nauth: password\npassword: {self.user.server_passwd}\ncert: false\n")

        with tarfile.open('/srv/jupyterhub/configs/config_{}.tar'.format(self.user.escaped_name), mode='w') as tar:
            tar.add('/srv/jupyterhub/configs/config_{}.yaml'.format(self.user.escaped_name), arcname='config.yaml')
        data = open('/srv/jupyterhub/configs/config_{}.tar'.format(self.user.escaped_name), 'rb').read()
        container.put_archive('/home/coder/.config/code-server', data)    

        #remove temporary config files
        os.remove('/srv/jupyterhub/configs/config_{}.tar'.format(self.user.escaped_name))
        os.remove('/srv/jupyterhub/configs/config_{}.yaml'.format(self.user.escaped_name))

        with open('/srv/jupyterhub/src_files/vscode.html', mode='r') as f_vs:
            data = f_vs.read()
            final_data = data.replace('$store_url', self.public_domain + "socket" + self.proxy_spec).replace('$socket_path', "/socket" + self.proxy_spec + "socket")
            with open('/srv/jupyterhub/configs/vscode_{}.html'.format(self.user.escaped_name), mode='w')as f_cs:
                f_cs.write(final_data)

        with tarfile.open('/srv/jupyterhub/configs/vscode_{}.tar'.format(self.user.escaped_name), mode='w') as tar:
            tar.add('/srv/jupyterhub/configs/vscode_{}.html'.format(self.user.escaped_name), arcname='vscode.html')
        data = open('/srv/jupyterhub/configs/vscode_{}.tar'.format(self.user.escaped_name), 'rb').read()
        container.put_archive('/usr/lib/code-server/src/browser/pages/', data)    

        #remove temporary config files
        os.remove('/srv/jupyterhub/configs/vscode_{}.tar'.format(self.user.escaped_name))
        os.remove('/srv/jupyterhub/configs/vscode_{}.html'.format(self.user.escaped_name))

        # TODO: handle unpause
        self.log.info(
            "Starting %s %s (id: %s)",
            self.object_type,
            self.object_name,
            self.container_id[:7],
        )

        
        # start the container
        await self.start_object()


        if self.post_start_cmd:
            await self.post_start_exec()

        ip, port = await self.get_ip_and_port()

        if jupyterhub.version_info < (0, 7):
            # store on user for pre-jupyterhub-0.7:
            self.user.server.ip = ip
            self.user.server.port = port
        # jupyterhub 0.7 prefers returning ip, port:
        return (ip, port)

    @property
    def internal_hostname(self):
        """Return our hostname

        used with internal SSL
        """
        return self.container_name

    async def get_ip_and_port(self):
        """Queries Docker daemon for container's IP and port.

        If you are using network_mode=host, you will need to override
        this method as follows::

            async def get_ip_and_port(self):
                return self.host_ip, self.port

        You will need to make sure host_ip and port
        are correct, which depends on the route to the container
        and the port it opens.
        """
        if self.use_internal_hostname:
            # internal ssl uses hostnames,
            # required for domain-name matching with internal SSL
            # TODO: should we always do this?
            # are there any cases where internal_ip works
            # and internal_hostname doesn't?
            ip = self.internal_hostname
            port = self.port
        elif self.use_internal_ip:
            resp = await self.docker("inspect_container", self.container_id)
            network_settings = resp["NetworkSettings"]
            if "Networks" in network_settings:
                ip = self.get_network_ip(network_settings)
            else:  # Fallback for old versions of docker (<1.9) without network management
                ip = network_settings["IPAddress"]
            port = self.port
        else:
            resp = await self.docker("port", self.container_id, self.port)
            if resp is None:
                raise RuntimeError("Failed to get port info for %s" % self.container_id)

            ip = resp[0]["HostIp"]
            port = int(resp[0]["HostPort"])

        if ip == "0.0.0.0":
            ip = urlparse(self.client.base_url).hostname
            if ip == "localnpipe":
                ip = "localhost"

        return ip, port

    def get_network_ip(self, network_settings):
        networks = network_settings["Networks"]
        if self.network_name not in networks:
            raise Exception(
                "Unknown docker network '{network}'."
                " Did you create it with `docker network create <name>`?".format(
                    network=self.network_name
                )
            )

        network = networks[self.network_name]
        ip = network["IPAddress"]
        return ip

    async def stop(self, now=False):
        """Stop the container

        Will remove the container if `c.DockerSpawner.remove` is `True`.

        Consider using pause/unpause when docker-py adds support.
        """
        self.log.info(
            "Stopping %s %s (id: %s)",
            self.object_type,
            self.object_name,
            self.object_id[:7],
        )
        await self.stop_object()

        if self.remove:
            await self.remove_object()

        self.clear_state()

    def _volumes_to_binds(self, volumes, binds, mode="rw"):
        """Extract the volume mount points from volumes property.

        Returns a dict of dict entries of the form::

            {'/host/dir': {'bind': '/guest/dir': 'mode': 'rw'}}
        """

        def _fmt(v):
            return self.format_volume_name(v, self)

        for k, v in volumes.items():
            m = mode
            if isinstance(v, dict):
                if "mode" in v:
                    m = v["mode"]
                v = v["bind"]
            binds[_fmt(k)] = {"bind": _fmt(v), "mode": m}
        return binds

    def _render_templates(self, obj, ns=None):
        """Recursively render template strings

        Dives down into dicts, lists, tuples
        and applies template formatting on all strings found in:
        - list or tuple items
        - dict keys or values

        Always returns the original object structure.
        """
        if ns is None:
            ns = self.template_namespace()

        _fmt = partial(self._render_templates, ns=ns)

        if isinstance(obj, str):
            try:
                return obj.format(**ns)
            except (ValueError, KeyError):
                # not a valid format string
                # to avoid crashing leave invalid templates unrendered
                # otherwise, this unconditional formatting would not allow
                # strings with `{` characters in them
                return obj
        elif isinstance(obj, dict):
            return {_fmt(key): _fmt(value) for key, value in obj.items()}
        elif isinstance(obj, (list, tuple)):
            return type(obj)([_fmt(item) for item in obj])
        else:
            return obj

