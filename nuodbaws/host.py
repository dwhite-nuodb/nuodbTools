import boto.ec2
import boto.route53
from paramiko import SSHClient, SFTPClient
import base64, inspect, json, os, socket, string, sys, tempfile, time

class NuoDBhost:
  def __init__(self, name, EC2Connection, Route53Connection, dns_domain,
               advertiseAlt=False, agentPort=48004, altAddr="",
               domain="domain", domainPassword="bird", enableAutomation=False,
               enableAutomationBootstrap=False,
               isBroker=False, portRange=48005, peers=[],
               ssh_user="ec2-user", region="default"):
    args, _, _, values = inspect.getargvalues(inspect.currentframe())
    for i in args:
      setattr(self, i, values[i])
    self.exists = False

    for reservation in self.EC2Connection.get_all_reservations():
      for instance in reservation.instances:
        if instance.__dict__['tags']['Name'] == name and instance.state == 'running':
          self.exists = True
          self.instance = instance
          self.update_data()              

  def agent_action(self, action):
    command = "sudo service nuoagent " + action
    if self.ssh_execute(command) != 0:
      return "Failed ssh execute on command " + command
  def apply_license(self, nuodblicense):
        if not self.isBroker:
            return "Can only apply a license to a node that is a Broker"
        f = tempfile.NamedTemporaryFile()
        f.write(nuodblicense)
        f.seek(0)
        self.scp(f.name, "/tmp/license.file")
        f.close()
        for command in [" ".join(["/opt/nuodb/bin/nuodbmgr --broker localhost --password", self.domainPassword, "--command \"apply domain license licenseFile /tmp/license.file\""])]:
            if self.ssh_execute(command) != 0:
                sys.exit("Failed ssh execute on command " + command)
                
  def attach_volume(self, size, mount_point):
        if not self.exists:
            return("Node " + self.name + " doesn't exist")
        device = ""
        # find suitable device
        for letter in list(string.ascii_lowercase):
            if len(device) == 0:
                command = "ls /dev | grep sd" + letter
                if self.ssh_execute(command) > 0:
                    device = "/".join(["", "dev", "sd" + letter])
                    print "Found " + device
        if len(device) == 0:
            sys.exit("Could not find a suitable device for mounting")
        volume = self.EC2Connection.create_volume(size, self.region)
        while volume.status != "available":
            volume.update()
            time.sleep(1)
        self.EC2Connection.attach_volume(volume.id, self.instance.id, device)
        while volume.attachment_state() != "attached":
            volume.update()
            time.sleep(1)
        for command in ["sudo mkdir -p " + mount_point, " ".join(["sudo", "mkfs", "-t ext4", device]), " ".join(["sudo", "mount", device, mount_point])]:
            print command
            self.ssh_execute(command)
        return self.ssh_execute("mount | grep " + mount_point)
        
  def console_action(self, action):
        command = "sudo service nuoautoconsole " + action
        if self.ssh_execute(command) != 0:
                return "Failed ssh execute on command " + command
            
  def create(self, ami, key_name, instance_type, getPublicAddress=False, security_groups=None, security_group_ids=None, subnet=None, ChefUserData = None):
        if not self.exists:
            userdata = {"hostname": ".".join([self.name, self.dns_domain])}
            if ChefUserData != None:
              self.chefdata = ChefUserData
              userdata['chef'] = ChefUserData
            interface = boto.ec2.networkinterface.NetworkInterfaceSpecification(subnet_id=subnet, groups=security_group_ids, associate_public_ip_address=getPublicAddress)
            interface_collection = boto.ec2.networkinterface.NetworkInterfaceCollection(interface)
            reservation = self.EC2Connection.run_instances(ami, key_name=key_name, instance_type=instance_type, user_data=base64.b64encode(json.dumps(userdata)), network_interfaces=interface_collection) 
            self.exists = True
            for instance in reservation.instances:
                self.instance = instance
                self.update_data()
                instance.add_tag("Name", self.name)
            return self
        else:
            print("Node " + self.name + " already exists. Not starting again.")
            self.update_data()
            return self
    
  def dns_delete(self):
        self.update_data()
        zone = self.Route53Connection.get_zone(self.dns_domain)
        for fqdn in [self.int_fqdn, self.ext_fqdn]:
            if zone.find_records(fqdn, "A") != None:
                zone.delete_a(fqdn)
                
  def dns_set(self):
        self.update_data()
        zone = self.Route53Connection.get_zone(self.dns_domain)
        for fqdn, ipaddress in {self.int_fqdn: self.int_ip, self.ext_fqdn: self.ext_ip}.iteritems():
            if zone.find_records(fqdn, "A") != None:
                zone.update_a(fqdn, value=ipaddress, ttl=60)
            else:
                zone.add_a(fqdn, value=ipaddress)
                
  def __get_ssh_connection(self):
        try:
            host = socket.gethostbyname(self.ext_fqdn)
        except:
            host = self.ext_ip
        self.ssh_connection = SSHClient()
        self.ssh_connection.set_missing_host_key_policy(TemporaryAddPolicy())
        # self.ssh_connection.load_system_host_keys()
        self.ssh_connection.connect(host, username=self.ssh_user)
 
  def health(self):
        return self.ssh_execute("sudo service nuoagent status")
    
  def provision(self, peers=[], enableAutomation=False, enableAutomationBootstrap=False, templateFiles=["default.properties", "nuodb-rest-api.yml", "webapp.properties"]):
        if len(peers) > 0:
            self.peers = peers
        if enableAutomation != self.enableAutomation:
            self.enableAutomation = enableAutomation
        if enableAutomationBootstrap != self.enableAutomationBootstrap:
            self.enableAutomationBootstrap = enableAutomationBootstrap
        self.update_data()
        self.scp(local_file="./templates/yum/nuodb.repo", remote_file="/tmp/nuodb.repo")
        for command in [ 'sudo hostname ' + self.ext_fqdn, 'sudo mv /tmp/nuodb.repo /etc/yum.repos.d/', 'sudo yum -y install nuodb']:
            if self.ssh_execute(command) != 0:
                return "Failed ssh execute on command " + command
        properties = dict(
            advertiseAlt=str(self.advertiseAlt).lower(),
            agentPort=self.agentPort,
            # altAddr=str(self.ext_fqdn),
            altAddr=str(self.ext_ip),
            domain=self.domain,
            domainPassword=self.domainPassword,
            enableAutomation=str(self.enableAutomation).lower(),
            enableAutomationBootstrap=str(self.enableAutomationBootstrap).lower(),
            isBroker=str(self.isBroker).lower(),
            peers=",".join(self.peers),
            portRange=self.portRange,
            region=self.region
            )
        for templateFile in templateFiles:
            templateContentFile = open("./templates/" + templateFile, "r")
            props = string.Template(templateContentFile.read())
            output = props.substitute(properties)
            f = tempfile.NamedTemporaryFile()
            f.write(str(output))
            templateContentFile.close()
            f.seek(0)
            self.scp(f.name, "/tmp/" + templateFile)
            f.close()
            command = "sudo mv /tmp/" + templateFile + " /opt/nuodb/etc/" + templateFile
            if self.ssh_execute(command) != 0:
                return "Failed ssh execute on command " + command
        return "OK"      

  def scp(self, local_file, remote_file):    
        if not hasattr(self, 'ssh_connection'):
            self.__get_ssh_connection()
        sftp = SFTPClient.from_transport(self.ssh_connection.get_transport())
        sftp.put(local_file, remote_file)

  def ssh_execute(self, command, logfile="/tmp/paramiko.log"):
        if not hasattr(self, 'ssh_connection'):
            self.__get_ssh_connection()
        channel = self.ssh_connection.get_transport().open_session()
        channel.exec_command(command + " &>> /tmp/paramiko.log")
        return channel.recv_exit_status() 
  def status(self):
        if self.exists:
            self.update_data()
            return self.instance.state
        else:
            return "Host does not exist"
  def terminate(self):
        if self.exists:
            # self.dns_delete()
            self.EC2Connection.terminate_instances(self.id)
            self.exists = False
            return("Terminated " + self.name)
        else:
            return("Cannot terminate " + self.name + " as node does not exist.")
  def update_data(self):
        self.instance.update()
        self.id = self.instance.id
        self.ext_ip = self.instance.ip_address
        self.int_ip = self.instance.private_ip_address
        self.int_fqdn = ".".join([self.name, "int", self.dns_domain])
        self.ext_fqdn = ".".join([self.name, self.dns_domain])
        
class TemporaryAddPolicy:
    def missing_host_key(self, client, hostname, key):
        pass