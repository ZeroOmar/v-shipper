# v-shipper: Docker Volume Migration Application

# Single source of truth for the app version. v-shipper and v-helper share a
# version line — both bump together on each coordinated release — so v-shipper
# can compare a connected v-helper's version against its own.
__version__ = "0.5.0"
