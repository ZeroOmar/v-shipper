# v-shipper: Docker Volume Migration Application

# Silence paramiko's noisy TripleDES CryptographyDeprecationWarning. paramiko is
# pulled in transitively by the docker SDK; the warning is harmless and clutters
# logs. Filter registered here (package import) so it's in place before any
# submodule imports paramiko.
import warnings as _warnings
_warnings.filterwarnings("ignore", message="TripleDES has been moved")

# Single source of truth for the app version. v-shipper and v-helper share a
# version line — both bump together on each coordinated release — so v-shipper
# can compare a connected v-helper's version against its own.
__version__ = "0.5.2"
