# Changelog

## [2.0.0] - 2024-06-17

### Changed

- When install local packages, their dependencies are also installed by default unless users choose to disable them via `--no-dep-dep` flag

## [1.7.5] - 2024-05-22

### Fixed

- Fix `sbt add` command to ignore error when adding a package that is already added

## [1.7.4] - 2024-05-10

### Fixed

- Fix not able to install Poetry packages using `tool.poetry.include` to include extra files (such as css, js, html) in the final package

## [1.7.3] - 2024-04-07

### Fixed

- Fix not loading ignore_directories/ignore_directory_names from config file

## [1.7.2] - 2024-04-07

### Added

- Print directories that are being scanned for packages

## [1.7.1] - 2024-04-07

### Fixed

- Fixed a bug of discovering same package twice (due to softlink)
