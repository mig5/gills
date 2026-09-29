%global gills_version 0.1.0

Name:           gills
Version:        %{gills_version}
Release:        1%{?dist}.gills1
Summary:        Watch APT and RPM repositories for package changes.

License:        GPL-3.0-or-later
URL:            https://git.mig5.net/mig5/gills
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-poetry-core

Requires: python3-yaml
Requires: python3-debian
Requires: python3-defusedxml
Requires: python3-zstandard

%description
Watch APT and RPM repositories for package changes.

%prep
%autosetup -n gills 

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files gills 

%files -f %{pyproject_files}
%license LICENSE
%doc README.md CHANGELOG.md
%{_bindir}/gills

%changelog
* Tue Sep 29 2026 Miguel Jacq <mig@mig5.net> - 0.1.0-1
- Initial release
