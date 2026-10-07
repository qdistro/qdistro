Name:           qdistro-product-dud
Version:        1.0
Release:        1
Summary:        Agama DUD payload injecting the qdistro product definition
License:        GPL-3.0-or-later
Source0:        qdistro.yaml
BuildArch:      noarch

%description
Driver Update Disk payload for the Agama live installer: drops
/usr/share/agama/products.d/qdistro.yaml into the live system before
agama-web-server probes products.d, so an autoinstall profile can select
product.id=qdistro. Delivered via inst.dud=; applied by the 99agama-dud
dracut module (RPM-format DUD: unpacked with rpm2cpio, contents copied
into the live root verbatim).

%prep
# nothing to unpack; Source0 is copied in %install

%build
# nothing to build

%install
mkdir -p %{buildroot}/usr/share/agama/products.d
cp %{SOURCE0} %{buildroot}/usr/share/agama/products.d/qdistro.yaml

%files
/usr/share/agama/products.d/qdistro.yaml
