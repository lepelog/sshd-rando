from collections import Counter, OrderedDict
import yaml
import random
import logging


class SettingInfoError(RuntimeError):
    pass


class SettingType:
    NONE: int = 0
    STANDARD: int = 1
    COSMETIC: int = 2
    PREFERENCE: int = 3
    OTHER: int = 4

    @staticmethod
    def from_str(name: str) -> int:
        match name:
            case "Standard":
                return SettingType.STANDARD
            case "Cosmetic":
                return SettingType.COSMETIC
            case "Preference":
                return SettingType.PREFERENCE
            case "Other":
                return SettingType.OTHER
            case _:
                raise SettingInfoError(f"Unknown setting type {name}")


# Info about a setting
class SettingInfo:
    def __init__(self) -> None:
        self.name: str = None
        self.pretty_name: str = None
        self.type: int = None
        self.options: list[str] = []
        self.pretty_options: list[str] = []
        self.descriptions: list[str] = []
        self.default_option: int = 0

        self.has_random_option: bool = True
        self.random_option: str = None  # name of random option
        self.random_low: int = 0  # lower bound when choosing random option
        self.random_high: int = 0  # upper bound when choosing random option

    def __str__(self) -> str:
        return self.pretty_name | self.name


settings_info_map: OrderedDict[str, SettingInfo] = {}


# Setting for a specific world
class Setting:
    def __init__(
        self, name_: str = None, value_: str = None, info_: SettingInfo = None
    ) -> None:
        self.name: str = name_
        self.value: str = value_
        self.info: SettingInfo = info_
        self.is_using_random_option: bool = False
        self.current_option: int = 0
        self.custom_value: str = None

    def __str__(self) -> str:
        return self.info.pretty_name
    
    def resolve_if_random(self) -> None:
        if self.value == self.info.random_option:
            self.is_using_random_option = True
            random_options = self.info.options[self.info.random_low:self.info.random_high + 1]
            self.value = random.choice(random_options)
            logging.getLogger("").debug(f"Chose {self.value} as random option for {self.name}")


class SettingMap:
    def __init__(self) -> None:
        self.settings: dict[str, Setting] = {}
        self.starting_inventory: Counter[str] = Counter()
        self.excluded_locations: set = set()
        self.mixed_entrance_pools: list[list[str]] = []


# Helper class to allow for automatic sanity checking and syntactic sugar when
# checking setting values
class SettingGet:
    def __init__(self, name_: str, setting_: Setting) -> None:
        self.setting_name: str = name_
        self.setting: Setting = setting_

    def value(self) -> str:
        return self.setting.value

    def pretty_value(self) -> str:
        return self.setting.info.pretty_options[self.value_index()]

    def value_index(self, value: str = None) -> int:
        if value == None:
            value = self.setting.value
        return self.setting.info.options.index(value)

    def pretty_name(self) -> str:
        return self.setting.info.pretty_name

    def get_custom_value(self) -> str:
        return self.setting.custom_value

    def set_custom_value(self, value_: str):
        self.setting.custom_value = value_

    def value_as_number(self):
        return int(self.setting.value)

    def __eq__(self, value_: str) -> bool:
        if value_ not in self.setting.info.options:
            raise SettingInfoError(
                f'ERROR: "{value_}" is not a known value for {self.setting_name}'
            )
        return self.setting.value == value_

    def is_any_of(self, *values) -> bool:
        for value in values:
            if self == value:
                return True
        return False


def get_all_settings_info() -> dict[str, SettingInfo]:
    # Load in settings if we haven't done so yet
    if len(settings_info_map) == 0:
        print("Loading setting data")
        path = "data/settings_list.yaml"
        with open(path, "r") as settings_file:
            settings_yaml = yaml.safe_load(settings_file)
            for setting_node in settings_yaml:
                # Check for required fields
                for field in [
                    "name",
                    "default_option",
                    "pretty_name",
                    "pretty_options",
                    "options",
                ]:
                    if field not in setting_node:
                        raise SettingInfoError(
                            f"Setting \"{setting_node['name']}\" is missing required field \"{field}\""
                        )

                name_str = setting_node["name"]
                pretty_name_str = setting_node["pretty_name"]
                default_option = str(setting_node["default_option"])

                # Assume a standard setting if there's no specification
                setting_type = SettingType.STANDARD
                if "type" in setting_node:
                    setting_type = SettingType.from_str(setting_node["type"])

                # If multiple settings were defined in one node, split them up
                names = name_str.split(",")
                pretty_names = pretty_name_str.split(",")
                default_options = default_option.split(",")
                pretty_options = []
                for pretty_option in setting_node["pretty_options"]:
                    # If a range is being specified, add everything in the range
                    if "-" in pretty_option:
                        lower_bound, upper_bound = [
                            int(bound) for bound in pretty_option.split("-")
                        ]
                        pretty_options.extend(
                            str(o) for o in list(range(lower_bound, upper_bound + 1))
                        )
                    else:
                        pretty_options.append(pretty_option)

                descriptions = []
                options = []
                # Also split up multiple options with the same description
                for option in setting_node["options"]:
                    for option_name, description in option.items():
                        option_names = option_name.split("/")
                        for op in option_names:
                            # If a range is being specified, add everything in the range
                            if "-" in op:
                                lower_bound, upper_bound = [
                                    int(bound) for bound in op.split("-")
                                ]
                                options.extend(
                                    str(o)
                                    for o in list(range(lower_bound, upper_bound + 1))
                                )
                                descriptions.extend(
                                    [description] * (upper_bound - lower_bound + 1)
                                )
                            else:
                                options.append(op)
                                descriptions.append(description)

                assert len(pretty_names) == len(names)
                assert len(options) == len(descriptions) and len(options) == len(
                    pretty_options
                )

                # Insert all the data now
                for i in range(len(names)):
                    settings_info_map[names[i]] = SettingInfo()
                    s = settings_info_map[names[i]]
                    s.name = names[i]
                    s.pretty_name = pretty_names[i]
                    s.type = setting_type
                    s.default_option = options.index(
                        default_options[min(len(default_options) - 1, i)]
                    )
                    s.options = options
                    s.pretty_options = pretty_options
                    s.descriptions = descriptions

                    if "no_autogenerate_random" in setting_node:
                        s.has_random_option = False
                        continue

                    # Get alias for random choice
                    if "random_alias" in setting_node:
                        s.random_option = setting_node["random_alias"]
                    else:
                        s.random_option = "random"

                    # Set the range of options the random option can pick from
                    if "random_range" in setting_node:
                        # Check to make sure necessary fields exist
                        for field in ["first", "last"]:
                            if field not in setting_node["random_range"]:
                                raise SettingInfoError(
                                    f'Missing field "{field}" in random_range for "{s.name}"'
                                )
                        first = setting_node["random_range"]["first"]
                        last = setting_node["random_range"]["last"]
                        s.random_low = options.index(first)
                        s.random_high = options.index(last)
                    # If no range is specified, use all options
                    else:
                        s.random_low = 0
                        s.random_high = len(options) - 1

                    # Add the random option if it's not already specified
                    if s.random_option not in s.options:
                        # This should only apply to non-aliased random selections currently
                        s.options.append(s.random_option)
                        s.pretty_options.append("Random")
                        s.descriptions.append("")

    return settings_info_map
